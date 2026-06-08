"""
jkanime.net download-link scraper (browserless, pure httpx).

Pipeline: jkanime episode HTML (base64-decoded server list)
          -> Mediafire file page (permanent URL)
          -> Mediafire signed direct URL (expires)
          -> wget; or yt-dlp fallback (mp4upload, streamtape, ...)

Per-series output layout:
  {series}/mediafire_pages.json  PRIMARY. Permanent Mediafire page URLs.
  {series}/links.json            Signed direct URLs (expire). Use soon.
  {series}/progress.json         Checkpoint, written per episode.
  {series}/download.sh           Generated wget/yt-dlp download script.
  {series}/playlist.m3u          Episodes in numeric order.
  {series}/1.mp4, 2.mp4 ...      Downloaded episodes.

Usage:
  python jkanime_scraper.py --url https://jkanime.net/yugioh-duel-monsters-gx/1/
  python jkanime_scraper.py --series yugioh-duel-monsters-gx --start 1 --end 180
"""

import argparse
import base64
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

BASE_URL = "https://jkanime.net"

# Ordered fallback hosts (yt-dlp compatible). mp4upload is the most reliable.
FALLBACK_PRIORITY = ["mp4upload", "streamtape", "mega", "voe", "mixdrop", "doodstream"]

SERVERS_RE = re.compile(r"var\s+servers\s*=\s*(\[.*?\]);\s*\n", re.DOTALL)
DIRECT_DL_RE = re.compile(
    r"https://download\d+\.mediafire\.com/[^\s\"'<>]+\.(?:mp4|mkv|avi|webm)",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-MX,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


# --- filesystem / checkpoint -------------------------------------------------

def series_dir(series: str) -> Path:
    d = Path(series)
    d.mkdir(exist_ok=True)
    return d


def load_progress(series: str) -> dict:
    f = series_dir(series) / "progress.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_progress(series: str, results: dict):
    d = series_dir(series)
    (d / "progress.json").write_text(
        json.dumps({"results": results}, indent=2, ensure_ascii=False)
    )
    # Export permanent Mediafire page URLs immediately (primary recovery file).
    pages = {ep: r["mediafire_page"] for ep, r in results.items() if r.get("mediafire_page")}
    (d / "mediafire_pages.json").write_text(json.dumps(pages, indent=2, ensure_ascii=False))


# --- server-list parsing -----------------------------------------------------

def decode_servers(servers_raw: list) -> dict[str, str]:
    """Map lowercased server name -> decoded URL. Decodes base64 once per entry."""
    out: dict[str, str] = {}
    for s in servers_raw:
        name = s.get("server", "").lower()
        if not name:
            continue
        try:
            url = base64.b64decode(s.get("remote", "")).decode().strip()
        except Exception:
            url = s.get("remote", "")
        out[name] = url
    return out


def pick_fallback(server_map: dict[str, str]) -> tuple[str, str] | tuple[None, None]:
    """Best available fallback host by priority."""
    for name in FALLBACK_PRIORITY:
        if name in server_map:
            return name, server_map[name]
    return None, None


# --- Mediafire ---------------------------------------------------------------

def resolve_mediafire_direct(client: httpx.Client, page_url: str) -> str | None:
    """Fetch a Mediafire file page and extract the signed direct download URL."""
    try:
        resp = client.get(page_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        m = DIRECT_DL_RE.search(resp.text)
        return m.group(0) if m else None
    except Exception:
        return None


# --- per-episode pipeline ----------------------------------------------------

def process_episode(client: httpx.Client, results: dict, series: str, ep: int) -> dict:
    key = str(ep)
    existing = results.get(key)

    # Already resolved (direct or fallback) — nothing to do.
    if existing and (existing.get("direct_url") or existing.get("fallback_url")):
        return existing

    # Have a permanent Mediafire page but no direct URL yet — retry direct only.
    if existing and existing.get("mediafire_page"):
        direct = resolve_mediafire_direct(client, existing["mediafire_page"])
        if direct:
            existing["direct_url"] = direct
            existing["error"] = None
            return existing
        # Fall through to a full re-scrape to obtain a fresh fallback URL.

    result = {
        "ep": ep,
        "mediafire_page": None,
        "direct_url": None,
        "fallback_url": None,
        "fallback_server": None,
        "error": None,
    }

    ep_url = f"{BASE_URL}/{series}/{ep}/"
    try:
        resp = client.get(ep_url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            result["error"] = f"jkanime_http_{resp.status_code}"
            return result
        html = resp.text
    except Exception as e:
        result["error"] = f"jkanime_err:{e}"
        return result

    m = SERVERS_RE.search(html)
    if not m:
        result["error"] = "no_servers_array"
        return result
    try:
        servers_raw = json.loads(m.group(1))
    except Exception:
        result["error"] = "invalid_servers_json"
        return result

    server_map = decode_servers(servers_raw)

    # Always record a fallback, independent of Mediafire availability.
    fb_server, fb_url = pick_fallback(server_map)
    result["fallback_server"] = fb_server
    result["fallback_url"] = fb_url

    mf_url = server_map.get("mediafire")
    if mf_url:
        result["mediafire_page"] = mf_url
        # Persist the permanent URL before attempting the signed direct link.
        results[key] = result
        save_progress(series, results)

        direct = resolve_mediafire_direct(client, mf_url)
        if direct:
            result["direct_url"] = direct
            result["error"] = None
            return result

    if fb_url:
        result["error"] = f"mediafire_failed->fallback:{fb_server}"
    else:
        result["error"] = "no_mediafire_no_fallback"
    return result


# --- series discovery --------------------------------------------------------

def detect_series_info(client: httpx.Client, url: str) -> tuple[str, int]:
    """Return (series_slug, total_episodes) from any episode URL."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        raise ValueError(f"invalid URL: {url}")
    series = parts[0]

    resp = client.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text

    anime_id_m = re.search(r"ajax/episodes/(\d+)/", html)
    if not anime_id_m:
        count_m = re.search(r"(\d+)\s*[Ee]pisodios?", html)
        return series, int(count_m.group(1)) if count_m else 0

    anime_id = anime_id_m.group(1)
    csrf_m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    token = csrf_m.group(1) if csrf_m else ""

    ajax_url = f"{BASE_URL}/ajax/episodes/{anime_id}/1"
    r = client.post(
        ajax_url,
        headers={**HEADERS, "X-Requested-With": "XMLHttpRequest", "Referer": url, "X-CSRF-TOKEN": token},
        data={"_token": token},
        timeout=15,
    )
    if r.status_code == 200:
        return series, r.json().get("total", 0)

    count_m = re.search(r"(\d+)\s*[Ee]pisodios?", html)
    return series, int(count_m.group(1)) if count_m else 0


# --- download script generation ----------------------------------------------

def generate_download_script(series: str, results: dict) -> Path:
    d = series_dir(series)
    lines = [
        "#!/usr/bin/env bash",
        "set -uo pipefail",
        f"# Downloads for: {series}",
        f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        'DIR="$(dirname "$0")"',
        'cd "$DIR"',
        "",
    ]
    for ep, data in sorted(results.items(), key=lambda x: int(x[0])):
        n = int(ep)
        fname = f"{n}.mp4"  # simple numbering for easy playlist handling
        direct = data.get("direct_url")
        fb_url = data.get("fallback_url")
        fb_server = data.get("fallback_server", "")

        lines.append(f'if [ -f "{fname}" ]; then')
        lines.append(f'  echo "[{n:>3}] skip"')
        lines.append("else")
        if direct:
            lines.append(f'  wget -q --show-progress --continue -O "{fname}" "{direct}" \\')
            lines.append(f'    && echo "[{n:>3}] OK wget" || echo "[{n:>3}] FAIL"')
        elif fb_url:
            lines.append(f'  echo "[{n:>3}] yt-dlp ({fb_server})"')
            lines.append(f'  yt-dlp -o "{fname}" --no-part "{fb_url}" \\')
            lines.append(f'    && echo "[{n:>3}] OK yt-dlp" || echo "[{n:>3}] FAIL"')
        else:
            lines.append(f'  echo "[{n:>3}] NO LINK"')
        lines.append("fi")
        lines.append("")

    script = d / "download.sh"
    script.write_text("\n".join(lines))
    script.chmod(0o755)
    return script


def generate_playlist(series: str, results: dict) -> Path:
    """Write an M3U playlist in true numeric episode order.

    Robust against lexical filename sorting (1, 10, 2, ...): players follow
    the explicit M3U order regardless of how the folder lists files.
    """
    d = series_dir(series)
    lines = ["#EXTM3U", f"# {series}"]
    for ep in sorted(results, key=int):
        n = int(ep)
        lines.append(f"#EXTINF:-1,{series} - {n}")
        lines.append(f"{n}.mp4")
    playlist = d / "playlist.m3u"
    playlist.write_text("\n".join(lines) + "\n")
    return playlist


# --- orchestration -----------------------------------------------------------

def scrape(series: str, start: int, end: int, delay_min: float, delay_max: float):
    results = load_progress(series).get("results", {})

    have_pages = sum(1 for r in results.values() if r.get("mediafire_page"))
    have_direct = sum(1 for r in results.values() if r.get("direct_url"))
    print(f"Series: {series} | episodes {start}-{end}")
    print(f"Output: {series_dir(series).resolve()}")
    print(f"Checkpoint: {have_pages} mediafire_page | {have_direct} direct_url")
    print("-" * 60)

    with httpx.Client(follow_redirects=True) as client:
        for ep in range(start, end + 1):
            key = str(ep)
            if key in results and results[key].get("direct_url"):
                print(f"[{ep:>3}] cached")
                continue

            mode = "refresh" if (key in results and results[key].get("mediafire_page")) else "full"
            print(f"[{ep:>3}] [{mode}] ...", end=" ", flush=True)

            result = process_episode(client, results, series, ep)
            results[key] = result
            save_progress(series, results)

            if result["direct_url"]:
                print(f"OK wget:{result['direct_url'].split('/')[-1]}")
            elif result["fallback_url"]:
                print(f"OK yt-dlp:{result['fallback_server']}")
            else:
                print(f"FAIL {result['error']}")

            if ep < end:
                time.sleep(random.uniform(delay_min, delay_max))

    d = series_dir(series)
    direct_clean = {ep: r["direct_url"] for ep, r in results.items() if r.get("direct_url")}
    (d / "links.json").write_text(json.dumps(direct_clean, indent=2, ensure_ascii=False))

    script = generate_download_script(series, results)
    playlist = generate_playlist(series, results)

    fallback_eps = {
        ep: r["fallback_server"]
        for ep, r in results.items()
        if r.get("fallback_url") and not r.get("direct_url")
    }
    failed = {
        ep: r.get("error")
        for ep, r in results.items()
        if not r.get("direct_url") and not r.get("fallback_url")
    }
    total = end - start + 1

    print("\n" + "=" * 60)
    print(f"wget (Mediafire direct): {len(direct_clean)}/{total}")
    print(f"yt-dlp (fallback):       {len(fallback_eps)}/{total}  {sorted(set(fallback_eps.values()))}")
    print(f"no link:                 {len(failed)}")
    for ep, err in list(failed.items())[:10]:
        print(f"  ep {ep}: {err}")
    print(f"\n[dir]     {d.resolve()}/")
    print(f"[primary] {d}/mediafire_pages.json")
    print(f"[links]   {d}/links.json")
    print(f"[script]  {script}")
    print(f"[m3u]     {playlist}")


def main():
    parser = argparse.ArgumentParser(description="jkanime.net download-link scraper")
    parser.add_argument("--url", help="any episode URL; auto-detects slug and total")
    parser.add_argument("--series", default=None, help="series slug (alternative to --url)")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--delay-min", type=float, default=0.5)
    parser.add_argument("--delay-max", type=float, default=1.5)
    args = parser.parse_args()

    if args.url:
        with httpx.Client(follow_redirects=True) as c:
            print(f"Detecting series: {args.url}")
            series, total = detect_series_info(c, args.url)
        end = args.end or total
        print(f"-> {series} | {total} episodes | scraping {args.start}-{end}\n")
        scrape(series, args.start, end, args.delay_min, args.delay_max)
    elif args.series:
        if not args.end:
            parser.error("--end is required without --url")
        scrape(args.series, args.start, args.end, args.delay_min, args.delay_max)
    else:
        parser.error("provide --url or --series")


if __name__ == "__main__":
    main()
