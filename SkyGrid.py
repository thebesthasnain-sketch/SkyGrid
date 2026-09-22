import json
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pygame

pygame.init()
pygame.font.init()

# OpenSky API Credentials
CLIENT_ID = "YOUR_CLIENT_ID_HERE"
CLIENT_SECRET = "YOUR_CLIENT_SECRET_HERE"

access_token = None
token_expires_at = 0

WIDTH, HEIGHT = 1280, 720
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("SkyGrid")

# Fonts are created ONCE (creating them every frame is slow)
font = pygame.font.SysFont("monospace", 11, bold=True)
hud_font = pygame.font.SysFont("monospace", 13, bold=True)

# --- SEARCH BAR STATE ---
search_text = ""
search_active = False

# Camera State (Zoom & Pan)
zoom = 1.0
min_zoom, max_zoom = 1.0, 50.0
pan_x, pan_y = 0, 0

# Dragging state
dragging = False
drag_anchor_x, drag_anchor_y = 0, 0

BASE_WORLD_SIZE = WIDTH

# --- TELEMETRY HUD: SELECTION STATE (selected_flight is an icao24 id) ---
selected_flight = None

# Shared Data and Thread Synchronization
live_flights = []
data_version = 0  # bumped by the worker whenever a new batch arrives
data_lock = threading.Lock()

# Flight LERP State Engine (keyed by icao24, NOT callsign)
flight_states = {}
UPDATE_INTERVAL = 30.0

# --- VECTOR AIRCRAFT ICON GENERATOR ---
def create_aircraft_icon(size=18, color=(0, 255, 150)):
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    points = [
        (size // 2, 0),
        (size // 2 + 3, size // 3),
        (size - 1, size // 2 + 2),
        (size // 2 + 3, size // 2 + 2),
        (size // 2 + 2, size - 1),
        (size // 2, size - 3),
        (size // 2 - 2, size - 1),
        (size // 2 - 3, size // 2 + 2),
        (1, size // 2 + 2),
        (size // 2 - 3, size // 3),
    ]
    pygame.draw.polygon(surf, color, points)
    return surf

BASE_PLANE_ICON = create_aircraft_icon(size=18, color=(0, 255, 150))
SELECTED_PLANE_ICON = create_aircraft_icon(size=18, color=(255, 255, 0))

# Rotated icons cached in 5-degree steps instead of rotating every frame
icon_cache = {}

def get_rotated_icon(selected, heading):
    step = int(round(heading / 5.0)) % 72
    key = (selected, step)
    icon = icon_cache.get(key)
    if icon is None:
        base = SELECTED_PLANE_ICON if selected else BASE_PLANE_ICON
        icon = pygame.transform.rotate(base, -step * 5)
        icon_cache[key] = icon
    return icon

# Callsign label surfaces cached instead of re-rendered every frame
label_cache = {}

def get_label(text):
    surf = label_cache.get(text)
    if surf is None:
        if len(label_cache) > 1000:
            label_cache.clear()
        surf = font.render(text, True, (255, 255, 255))
        label_cache[text] = surf
    return surf

# One reusable full-screen overlay for ALL trail segments
trail_surf = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)

# --- WEB MERCATOR COORDINATE CONVERSION ---
MAX_LAT = 85.05112878  # Mercator is undefined at the poles

def latlon_to_world(lat, lon):
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    world_x = ((lon + 180.0) / 360.0) * BASE_WORLD_SIZE
    lat_rad = math.radians(lat)
    world_y = (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * BASE_WORLD_SIZE
    return world_x, world_y

def gps_to_pxls(lat, lon):
    world_x, world_y = latlon_to_world(lat, lon)
    return int((world_x * zoom) + pan_x), int((world_y * zoom) + pan_y)

def zoom_at(mouse_x, mouse_y, new_zoom):
    global zoom, pan_x, pan_y

    new_zoom = max(min_zoom, min(max_zoom, new_zoom))
    if new_zoom == zoom:
        return

    map_x = (mouse_x - pan_x) / zoom
    map_y = (mouse_y - pan_y) / zoom

    pan_x = mouse_x - map_x * new_zoom
    pan_y = mouse_y - map_y * new_zoom
    zoom = new_zoom

# --- ASYNC TILE CACHE & DOWNLOAD MANAGER ---
CACHE_DIR = "tiles_cache"
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

tile_memory_cache = {}
scaled_cache = {}
download_queue = queue.Queue()
pending_downloads = set()
failed_tiles = {}          # tile_key -> time of last failure
RETRY_AFTER = 30.0         # don't re-request a failed tile every frame
queue_lock = threading.Lock()

def tile_downloader_worker():
    while True:
        z, x, y = download_queue.get()
        tile_key = f"{z}_{x}_{y}"
        disk_path = os.path.join(CACHE_DIR, f"{tile_key}.png")

        if not os.path.exists(disk_path):
            url = f"https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=cb1_3kf7_1_1fa7fb8c87a02b81d32ab32b"
            req = urllib.request.Request(url, headers={"User-Agent": "PygameFlightRadar/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    image_data = response.read()
                    with open(disk_path, "wb") as f:
                        f.write(image_data)
            except Exception:
                failed_tiles[tile_key] = time.time()

        with queue_lock:
            pending_downloads.discard(tile_key)

        download_queue.task_done()

for _ in range(4):
    threading.Thread(target=tile_downloader_worker, daemon=True).start()

def get_tile_surface(z, x, y):
    tile_key = f"{z}_{x}_{y}"

    if tile_key in tile_memory_cache:
        return tile_memory_cache[tile_key]

    disk_path = os.path.join(CACHE_DIR, f"{tile_key}.png")
    if os.path.exists(disk_path):
        try:
            surface = pygame.image.load(disk_path).convert_alpha()
            if len(tile_memory_cache) > 600:
                tile_memory_cache.clear()
                scaled_cache.clear()
            tile_memory_cache[tile_key] = surface
            return surface
        except Exception:
            # Corrupt/partial file: delete it so it gets re-downloaded
            try:
                os.remove(disk_path)
            except OSError:
                pass

    with queue_lock:
        recently_failed = (time.time() - failed_tiles.get(tile_key, 0)) < RETRY_AFTER
        if tile_key not in pending_downloads and not recently_failed:
            pending_downloads.add(tile_key)
            download_queue.put((z, x, y))

    return None

def get_scaled_tile(z, x, y, size):
    """Return the tile scaled to `size` px, caching the scaled copy so we
    don't rescale every tile on every frame."""
    key = (z, x, y, size)
    scaled = scaled_cache.get(key)
    if scaled is not None:
        return scaled

    base = get_tile_surface(z, x, y)
    if base is None:
        return None

    if base.get_width() == size:
        scaled = base
    else:
        if len(scaled_cache) > 400:
            scaled_cache.clear()
        scaled = pygame.transform.scale(base, (size, size))
    scaled_cache[key] = scaled
    return scaled

# --- OPENSKY AUTHENTICATION WORKER ---
def get_opensky_token():
    global access_token, token_expires_at
    if not CLIENT_ID or not CLIENT_SECRET:
        return None  # anonymous access
    if access_token and time.time() < token_expires_at - 60:
        return access_token

    token_url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        print("[DEBUG] Requesting OAuth access token from OpenSky...")
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode())
            access_token = res_data.get("access_token")
            expires_in = res_data.get("expires_in", 1800)
            token_expires_at = time.time() + expires_in
            print("[SUCCESS] OAuth Token retrieved!")
            return access_token
    except Exception as e:
        print("[ERROR] Failed to obtain OAuth token:", e)
        return None

def bkg_api_worker():
    global live_flights, data_version
    url = "https://opensky-network.org/api/states/all"

    while True:
        try:
            token = get_opensky_token()
            headers = {"User-Agent": "Mozilla/5.0"}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            req = urllib.request.Request(url, headers=headers)

            print("[DEBUG] Polling OpenSky Network...")
            with urllib.request.urlopen(req, timeout=12) as response:
                data = json.loads(response.read().decode())
                states = data.get("states", [])

                fetched_flights = []
                if states:
                    print(f"[DEBUG] Raw aircraft array received: {len(states)} items")
                    for s in states[:300]:
                        icao = s[0]
                        has_callsign = bool(s[1] and s[1].strip())
                        callsign = s[1].strip() if has_callsign else icao.upper()
                        lon = s[5]
                        lat = s[6]
                        heading = s[10] if s[10] is not None else 0.0
                        speed = s[9]  # ground speed in m/s (may be None)
                        # barometric altitude in metres, falling back to geometric
                        altitude = s[7] if s[7] is not None else (s[13] if len(s) > 13 else None)
                        on_ground = bool(s[8])

                        if lat is not None and lon is not None:
                            fetched_flights.append({
                                "id": icao,
                                "callsign": callsign,
                                "has_callsign": has_callsign,
                                "lon": lon,
                                "lat": lat,
                                "heading": heading,
                                "speed": speed,
                                "altitude": altitude,
                                "on_ground": on_ground,
                            })

                if fetched_flights:
                    with data_lock:
                        live_flights = fetched_flights
                        data_version += 1
                    print(f"[SUCCESS] Loaded {len(fetched_flights)} valid aircraft!")

        except urllib.error.HTTPError as e:
            print(f"[ERROR] HTTP Error {e.code}: Check credentials or rate limit.")
            if e.code == 429:
                time.sleep(30)
        except Exception as e:
            print("[ERROR] Background Worker Failed:", e)

        time.sleep(UPDATE_INTERVAL)

threading.Thread(target=bkg_api_worker, daemon=True).start()

# --- ROUTE LOOKUP (origin / destination) ---
# OpenSky's live feed has no route info (ADS-B doesn't transmit it), so we look
# the callsign up in the free adsbdb.com route database, in the background,
# only for the flight you select. Results are cached per callsign.
route_cache = {}   # callsign -> {"status": loading|ok|none|error, "origin", "destination", "time"}
route_queue = queue.Queue()
ROUTE_RETRY_AFTER = 60.0

def format_airport(ap):
    if not ap:
        return "?"
    code = ap.get("iata_code") or ap.get("icao_code") or "?"
    place = ap.get("municipality") or ap.get("name") or ""
    return f"{code} {place}".strip()[:30]

def route_worker():
    while True:
        callsign = route_queue.get()
        result = {"status": "error", "time": time.time()}
        try:
            url = f"https://api.adsbdb.com/v0/callsign/{urllib.parse.quote(callsign)}"
            req = urllib.request.Request(url, headers={"User-Agent": "PygameFlightRadar/1.0"})
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.loads(response.read().decode())
            route = (data.get("response") or {}).get("flightroute") or {}
            if route.get("origin") or route.get("destination"):
                result = {
                    "status": "ok",
                    "origin": format_airport(route.get("origin")),
                    "destination": format_airport(route.get("destination")),
                    "time": time.time(),
                }
            else:
                result = {"status": "none", "time": time.time()}
        except urllib.error.HTTPError as e:
            if e.code == 404:  # callsign not in the database
                result = {"status": "none", "time": time.time()}
        except Exception:
            pass
        route_cache[callsign] = result
        route_queue.task_done()

for _ in range(2):
    threading.Thread(target=route_worker, daemon=True).start()

def request_route(callsign):
    """Queue a lookup unless we already have (or are fetching) an answer."""
    entry = route_cache.get(callsign)
    now = time.time()
    if entry is not None:
        if entry["status"] != "error":
            return
        if now - entry["time"] < ROUTE_RETRY_AFTER:
            return
    route_cache[callsign] = {"status": "loading", "time": now}
    route_queue.put(callsign)

def route_lines(info):
    if not info["has_callsign"]:
        return "FROM: N/A", "TO:   N/A"
    entry = route_cache.get(info["callsign"])
    if entry is None or entry["status"] == "loading":
        return "FROM: ...", "TO:   ..."
    if entry["status"] == "ok":
        return f"FROM: {entry['origin']}", f"TO:   {entry['destination']}"
    if entry["status"] == "none":
        return "FROM: UNKNOWN", "TO:   UNKNOWN"
    return "FROM: LOOKUP FAILED", "TO:   (RETRYING)"

def fmt_speed(ms):
    if ms is None:
        return "N/A"
    return f"{ms * 1.94384:.0f} KT ({ms * 3.6:.0f} KM/H)"

def fmt_altitude(info):
    if info["on_ground"]:
        return "ON GROUND"
    if info["altitude"] is None:
        return "N/A"
    return f"{info['altitude'] * 3.28084:,.0f} FT"

# Pre-built glass backgrounds (one per state) instead of allocating each frame
BAR_W, BAR_H = 320, 42
glass_cache = {}

def get_glass_surface(active):
    surf = glass_cache.get(active)
    if surf is None:
        radius = BAR_H // 2
        surf = pygame.Surface((BAR_W, BAR_H), pygame.SRCALPHA)
        bg_alpha = 200 if active else 150
        pygame.draw.rect(surf, (10, 15, 25, bg_alpha), (0, 0, BAR_W, BAR_H), border_radius=radius)
        border_color = (0, 255, 200, 220) if active else (255, 255, 255, 80)
        pygame.draw.rect(surf, border_color, (0, 0, BAR_W, BAR_H), width=1, border_radius=radius)
        pygame.draw.line(surf, (0, 255, 200, 200), (radius, 1), (BAR_W - radius, 1), width=2)
        glass_cache[active] = surf
    return surf

def draw_liquid_glass_bar(surface):
    bar_x = (WIDTH - BAR_W) // 2
    bar_y = 15

    surface.blit(get_glass_surface(search_active), (bar_x, bar_y))

    # Magnifying glass icon
    icon_center = (bar_x + 25, bar_y + BAR_H // 2)
    pygame.draw.circle(surface, (0, 255, 200), (icon_center[0] - 2, icon_center[1] - 2), 5, width=2)
    pygame.draw.line(surface, (0, 255, 200), (icon_center[0] + 2, icon_center[1] + 2), (icon_center[0] + 6, icon_center[1] + 6), width=2)

    if search_text:
        display_str = search_text + ("|" if (time.time() % 1.0 > 0.5 and search_active) else "")
        text_color = (255, 255, 255)
    else:
        display_str = "SEARCH CALLSIGN..." if not search_active else "|"
        text_color = (130, 145, 160)

    text_surf = hud_font.render(display_str, True, text_color)
    surface.blit(text_surf, (bar_x + 45, bar_y + (BAR_H - text_surf.get_height()) // 2))

def jump_to_flight(callsign):
    global pan_x, pan_y, zoom, selected_flight

    target_cs = callsign.strip().upper()

    for fid, info in flight_states.items():
        if info["callsign"].upper() == target_cs:
            selected_flight = fid
            zoom = 4.0
            world_x, world_y = latlon_to_world(info["curr_lat"], info["curr_lon"])
            pan_x = (WIDTH / 2) - (world_x * zoom)
            pan_y = (HEIGHT / 2) - (world_y * zoom)
            print(f"[RADAR] Jumped camera to target flight: {target_cs}")
            return

    print(f"[RADAR] Flight {target_cs} not found in current active radar batch!")

# Main Loop
running = True
clock = pygame.time.Clock()
seen_version = 0
bar_rect = pygame.Rect((WIDTH - BAR_W) // 2, 15, BAR_W, BAR_H)

while running:
    current_time = time.time()

    # --- EVENT PROCESSING ---
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False

        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:
                mx, my = event.pos

                if bar_rect.collidepoint(mx, my):
                    search_active = True
                else:
                    search_active = False

                    clicked_any = False
                    for fid, state in flight_states.items():
                        px, py = gps_to_pxls(state["curr_lat"], state["curr_lon"])
                        if (mx - px) ** 2 + (my - py) ** 2 <= 15 ** 2:
                            selected_flight = fid
                            clicked_any = True
                            break

                    if not clicked_any:
                        selected_flight = None
                        dragging = True
                        drag_anchor_x = mx - pan_x
                        drag_anchor_y = my - pan_y

        elif event.type == pygame.KEYDOWN:
            if search_active:
                if event.key == pygame.K_RETURN:
                    if search_text:
                        jump_to_flight(search_text)
                        search_active = False
                elif event.key == pygame.K_BACKSPACE:
                    search_text = search_text[:-1]
                elif event.key == pygame.K_ESCAPE:
                    search_active = False
                else:
                    if len(search_text) < 10 and event.unicode.isalnum():
                        search_text += event.unicode.upper()
            else:
                if event.key == pygame.K_ESCAPE:
                    selected_flight = None

        elif event.type == pygame.MOUSEWHEEL and not search_active:
            mx, my = pygame.mouse.get_pos()
            if event.y > 0:
                zoom_at(mx, my, zoom * 1.15)
            elif event.y < 0:
                zoom_at(mx, my, zoom / 1.15)

        elif event.type == pygame.MOUSEBUTTONUP:
            if event.button == 1:
                dragging = False

        elif event.type == pygame.MOUSEMOTION:
            if dragging and not search_active:
                mx, my = event.pos
                pan_x = mx - drag_anchor_x
                pan_y = my - drag_anchor_y

    # --- FULL-SCREEN DYNAMIC TILE RENDERING ---
    screen.fill((10, 15, 25))

    current_world_px = BASE_WORLD_SIZE * zoom
    tile_z = max(0, min(18, int(math.log2(current_world_px / 256.0))))

    num_tiles = 2 ** tile_z
    drawn_tile_size = current_world_px / num_tiles
    tile_w = int(math.ceil(drawn_tile_size))

    start_tx = max(0, int(-pan_x / drawn_tile_size))
    end_tx = min(num_tiles - 1, int((-pan_x + WIDTH) / drawn_tile_size))

    start_ty = max(0, int(-pan_y / drawn_tile_size))
    end_ty = min(num_tiles - 1, int((-pan_y + HEIGHT) / drawn_tile_size))

    for tx in range(start_tx, end_tx + 1):
        for ty in range(start_ty, end_ty + 1):
            draw_x = int((tx * drawn_tile_size) + pan_x)
            draw_y = int((ty * drawn_tile_size) + pan_y)
            tile_surf = get_scaled_tile(tile_z, tx, ty, tile_w)

            if tile_surf:
                screen.blit(tile_surf, (draw_x, draw_y))
            else:
                pygame.draw.rect(screen, (20, 25, 35), (draw_x, draw_y, tile_w, tile_w))
                pygame.draw.rect(screen, (30, 40, 55), (draw_x, draw_y, tile_w, tile_w), 1)

    mouse_x, mouse_y = pygame.mouse.get_pos()

    # --- APPLY NEW DATA (only when the worker delivered a new batch) ---
    new_batch = None
    with data_lock:
        if data_version != seen_version:
            new_batch = list(live_flights)
            seen_version = data_version

    if new_batch is not None:
        active_ids = {f["id"] for f in new_batch}
        flight_states = {k: v for k, v in flight_states.items() if k in active_ids}

        for flight in new_batch:
            fid = flight["id"]

            if fid not in flight_states:
                flight_states[fid] = {
                    "callsign": flight["callsign"],
                    "has_callsign": flight["has_callsign"],
                    "speed": flight["speed"],
                    "altitude": flight["altitude"],
                    "on_ground": flight["on_ground"],
                    "start_lat": flight["lat"],
                    "start_lon": flight["lon"],
                    "target_lat": flight["lat"],
                    "target_lon": flight["lon"],
                    "curr_lat": flight["lat"],
                    "curr_lon": flight["lon"],
                    "heading": flight.get("heading", 0.0),
                    "last_update": current_time,
                    "trail": [],
                }
            else:
                state = flight_states[fid]
                state["callsign"] = flight["callsign"]
                state["has_callsign"] = flight["has_callsign"]
                state["speed"] = flight["speed"]
                state["altitude"] = flight["altitude"]
                state["on_ground"] = flight["on_ground"]
                if state["target_lat"] != flight["lat"] or state["target_lon"] != flight["lon"]:
                    state["trail"].append((state["curr_lat"], state["curr_lon"]))
                    if len(state["trail"]) > 8:
                        state["trail"].pop(0)

                    state["start_lat"] = state["curr_lat"]
                    state["start_lon"] = state["curr_lon"]
                    state["target_lat"] = flight["lat"]
                    state["target_lon"] = flight["lon"]
                    state["heading"] = flight.get("heading", 0.0)
                    state["last_update"] = current_time

    if not flight_states:
        status_txt = font.render("CONNECTING TO OPENSKY RADAR NETWORK...", True, (255, 200, 0))
        screen.blit(status_txt, (20, 20))

    # --- PASS 1: interpolate positions + draw ALL trails onto one overlay ---
    trail_surf.fill((0, 0, 0, 0))
    visible = []

    for fid, state in flight_states.items():
        elapsed = current_time - state["last_update"]
        t = min(1.0, elapsed / UPDATE_INTERVAL)

        state["curr_lat"] = state["start_lat"] + t * (state["target_lat"] - state["start_lat"])
        state["curr_lon"] = state["start_lon"] + t * (state["target_lon"] - state["start_lon"])

        px, py = gps_to_pxls(state["curr_lat"], state["curr_lon"])

        if not (-30 <= px <= WIDTH + 30 and -30 <= py <= HEIGHT + 30):
            continue

        visible.append((fid, state, px, py))

        trail = state["trail"]
        if trail:
            pts = [gps_to_pxls(lat, lon) for lat, lon in trail]
            pts.append((px, py))
            n = len(pts)
            rgb = (255, 255, 0) if fid == selected_flight else (0, 255, 150)

            for i in range(n - 1):
                progress = (i + 1) / n
                alpha = int(40 + progress * 160)
                thickness = max(1, int(progress * 2.5))
                pygame.draw.line(trail_surf, (rgb[0], rgb[1], rgb[2], alpha), pts[i], pts[i + 1], thickness)

    screen.blit(trail_surf, (0, 0))  # one blit for every trail

    # --- PASS 2: planes, highlights, labels (drawn on top of trails) ---
    for fid, state, px, py in visible:
        is_selected = (fid == selected_flight)

        icon = get_rotated_icon(is_selected, state.get("heading", 0.0))
        screen.blit(icon, icon.get_rect(center=(px, py)))

        if is_selected:
            pygame.draw.circle(screen, (255, 255, 0), (px, py), 16, width=1)

        hovered = (px - mouse_x) ** 2 + (py - mouse_y) ** 2 <= 64

        if zoom >= 1.8 or hovered or is_selected:
            text_surface = get_label(state["callsign"])
            text_rect = text_surface.get_rect(topleft=(px + 12, py - 6))

            box_color = (200, 150, 0) if hovered else (10, 15, 25)
            pygame.draw.rect(screen, box_color, text_rect)
            screen.blit(text_surface, text_rect)

    # --- HUD OVERLAY RENDERING ---
    if selected_flight and selected_flight in flight_states:
        info = flight_states[selected_flight]

        if info["has_callsign"]:
            request_route(info["callsign"])  # no-op if cached / already loading
        from_txt, to_txt = route_lines(info)

        WHITE, CYAN = (255, 255, 255), (0, 255, 200)
        rows = [
            (f"LAT:  {info['curr_lat']:.4f}°", WHITE),
            (f"LON:  {info['curr_lon']:.4f}°", WHITE),
            (f"HDG:  {info['heading']:.0f}°", WHITE),
            (f"SPD:  {fmt_speed(info['speed'])}", WHITE),
            (f"ALT:  {fmt_altitude(info)}", WHITE),
            None,  # spacer before the route section
            (from_txt, CYAN),
            (to_txt, CYAN),
        ]

        hud_w = 270
        hud_h = 45 + sum(10 if r is None else 20 for r in rows)
        hud_surface = pygame.Surface((hud_w, hud_h), pygame.SRCALPHA)
        hud_surface.fill((10, 15, 25, 220))
        pygame.draw.rect(hud_surface, (0, 255, 150), hud_surface.get_rect(), width=1)

        hud_surface.blit(hud_font.render(f"FLIGHT: {info['callsign']}", True, (255, 255, 0)), (10, 10))

        y = 35
        for row in rows:
            if row is None:
                y += 10
                continue
            hud_surface.blit(font.render(row[0], True, row[1]), (10, y))
            y += 20

        screen.blit(hud_surface, (20, 20))

    # --- RENDER SEARCH BAR OVERLAY ---
    draw_liquid_glass_bar(screen)

    pygame.display.flip()
    clock.tick(60)

pygame.quit()