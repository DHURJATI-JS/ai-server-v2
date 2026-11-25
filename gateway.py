"""
Local AI gateway: ONE link for every device on your Wi-Fi / LAN.

Every request goes to THIS PC's model server first. It switches to the
OTHER PC when this one is:
  - busy      (all its slots are already answering requests)
  - overloaded (CPU on this PC above LOCAL_CPU_LIMIT)
  - offline   (model server not running / still loading)

Run:   double-click 3_start_gateway.bat   (or: python gateway.py)
Link:  http://<this PC's IP>:8000/v1       (OpenAI-compatible, for your Wi-Fi / LAN)
Check: http://<this PC's IP>:8000/status   (open in any browser on your LAN)

Two doors, two keys (both kept in gateway-keys.json next to this file - never in git):
  * port 8000, your LAN:   lan_key - the key your own devices and apps use. Unchanged:
    every endpoint is passed through exactly as before.
  * port 8001, this PC only (127.0.0.1): the door for the Cloudflare tunnel, i.e. the
    internet. It accepts ONLY requests SIGNED with internet_key, a long random key made on
    first start that nobody needs to see (the tunnel script hands it to your website,
    encrypted). The key itself never travels over the internet: each request carries an
    HMAC-SHA256 signature over its time, a one-time nonce and its body, so a copied request
    can't be replayed or changed, and every answer is signed back so the website knows it
    came from this gateway. /status and the API docs are not reachable through it.
Any request that arrives through Cloudflare (it carries Cloudflare's headers) gets the
internet door's rules on either port, so pointing the tunnel at the wrong port is still safe.

What the internet door lets in (everything else is refused before it reaches llama.cpp):
  * only POST /v1/chat/completions and GET /v1/models, correctly signed and at most
    5 minutes old (this PC's clock must be right), each signature used once;
  * only a small JSON body (256 KB), text-only messages, a short list of known fields -
    no files, uploads, images, audio, tools or other endpoints; the request is rebuilt
    from the checked fields, and none of the caller's headers are passed on;
  * at most 60 requests a minute; an address that sends wrong keys 10 times is blocked
    for 30 minutes; replies are trimmed to the answer (no version or timing details).
Nothing that comes in is ever saved to disk or run.
"""
import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

# ============================ settings ============================
GATEWAY_PORT = 8000                         # your Wi-Fi / LAN
INTERNET_PORT = 8001                        # Cloudflare tunnel only - listens on 127.0.0.1
SLOTS_PER_PC = 2                            # same as SLOTS in 2_start_server.bat
LOCAL_CPU_LIMIT = 90                        # % CPU; set to None to ignore CPU load
HEALTH_CHECK_SECONDS = 5
HERE = Path(__file__).resolve().parent
KEYS_FILE = HERE / "gateway-keys.json"      # git-ignored; made on first start

# The internet door (Cloudflare tunnel) - what it accepts. Your website needs far less than this.
INTERNET_MAX_BODY = 256 * 1024              # bytes per request
INTERNET_MAX_REPLY = 2 * 1024 * 1024        # bytes read back from the model server
INTERNET_MAX_TOKENS = 8192                  # longer answers are cut to this
INTERNET_PER_MINUTE = 60                    # requests a minute, all callers together
INTERNET_MAX_INFLIGHT = 4                   # requests at the same time
INTERNET_BAD_KEYS = 10                      # wrong keys from one address within 10 minutes ...
INTERNET_BLOCK_SECONDS = 30 * 60            # ... block that address for 30 minutes
INTERNET_CLOCK_WINDOW = 300                 # seconds a signed request may be old (or early)
# ==================================================================


def other_pc_ip() -> str:
    """The other PC's LAN address: asked at every logon by start_all.ps1 (env / other_pc_ip.txt)."""
    ip = (os.environ.get("OTHER_PC_IP") or "").strip()
    if not ip:
        try:
            ip = (HERE / "other_pc_ip.txt").read_text(encoding="utf-8", errors="ignore").split()[0]
        except (OSError, IndexError):
            ip = ""
    return ip


def load_keys() -> dict[str, str]:
    """Keys from gateway-keys.json (created on first start). Environment variables win.

    lan_key      - what your devices / apps send on your LAN (port 8000). Change it any time.
    internet_key - a long random key for the Cloudflare tunnel (port 8001). Never printed.
    server_key   - the model server's key; must equal API_KEY in 2_start_server.bat on BOTH PCs.
    """
    data: dict = {}
    try:
        data = json.loads(KEYS_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        data = {}
    changed = False
    if not isinstance(data.get("lan_key"), str):
        data["lan_key"] = secrets.token_urlsafe(24)
        changed = True
    if not isinstance(data.get("internet_key"), str) or len(data["internet_key"]) < 32:
        data["internet_key"] = secrets.token_urlsafe(48)
        changed = True
    if not isinstance(data.get("server_key"), str):
        data["server_key"] = "change-me-server-key"  # matches the default in 2_start_server.bat (LAN only)
        changed = True
    if changed:
        KEYS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {
        "lan": os.environ.get("GATEWAY_API_KEY", data["lan_key"]),
        "internet": os.environ.get("GATEWAY_INTERNET_KEY", data["internet_key"]),
        "server": os.environ.get("SERVER_API_KEY", data["server_key"]),
    }


KEYS = load_keys()
OTHER_PC_IP = other_pc_ip()


@dataclass(eq=False)
class Backend:
    name: str
    url: str
    max_inflight: int
    is_local: bool
    healthy: bool = False
    inflight: int = 0


backends = [Backend("this-pc", "http://127.0.0.1:8080", SLOTS_PER_PC, is_local=True)]
if OTHER_PC_IP:
    backends.append(Backend("other-pc", f"http://{OTHER_PC_IP}:8080", SLOTS_PER_PC, is_local=False))
local_cpu = 0.0
client: httpx.AsyncClient


async def health_loop() -> None:
    """Every few seconds: is each model server up and ready? How busy is this PC's CPU?"""
    global local_cpu
    psutil.cpu_percent(interval=None)  # prime the counter
    async with httpx.AsyncClient(timeout=3) as hc:
        while True:
            for b in backends:
                was = b.healthy
                try:
                    r = await hc.get(f"{b.url}/health")
                    b.healthy = r.status_code == 200   # 503 = still loading the model
                except httpx.HTTPError:
                    b.healthy = False
                if b.healthy != was:
                    print(f"[gateway] {b.name} is now {'ONLINE' if b.healthy else 'OFFLINE'}")
            local_cpu = psutil.cpu_percent(interval=None)
            await asyncio.sleep(HEALTH_CHECK_SECONDS)


def is_overloaded(b: Backend) -> bool:
    if b.inflight >= b.max_inflight:
        return True
    if b.is_local and LOCAL_CPU_LIMIT is not None and local_cpu >= LOCAL_CPU_LIMIT:
        return True
    return False


def choose(exclude: list[Backend]) -> Backend | None:
    online = [b for b in backends if b.healthy and b not in exclude]
    if not online:
        return None
    for b in online:                       # this PC first, then the other PC
        if not is_overloaded(b):
            return b
    # everyone is busy: queue on whichever is least loaded
    return min(online, key=lambda b: b.inflight / b.max_inflight)


def lan_ip() -> str:
    """This PC's address on your Wi-Fi / LAN. (Nothing is actually sent to 8.8.8.8.)"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def print_connection_info() -> None:
    ip = lan_ip()
    base = f"http://{ip}:{GATEWAY_PORT}/v1"
    print("=" * 64)
    print("  SEND YOUR OPENAI REQUESTS HERE (from any device on your network)")
    print(f"    base_url : {base}")
    print(f"    chat URL : {base}/chat/completions")
    print(f"    api_key  : {KEYS['lan'] or '(no key needed)'}   (lan_key in gateway-keys.json)")
    print('    model    : "gemma"')
    print(f"  Status page: http://{ip}:{GATEWAY_PORT}/status")
    print(f"  Internet door for the Cloudflare tunnel: http://127.0.0.1:{INTERNET_PORT} (this PC only).")
    print("    Its key stays in gateway-keys.json and is never shown.")
    print("=" * 64)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=3, read=None, write=60, pool=None))
    task = asyncio.create_task(health_loop())
    print_connection_info()
    yield
    task.cancel()
    await client.aclose()


# No API docs: nothing about this gateway is advertised, least of all through the tunnel.
app = FastAPI(title="Local AI gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

CLOUDFLARE_HEADERS = ("cf-ray", "cf-connecting-ip", "cdn-loop")


def via_internet(request: Request) -> bool:
    """True for the tunnel's port and for anything that came through Cloudflare."""
    server = request.scope.get("server") or ("", 0)
    if server[1] == INTERNET_PORT:
        return True
    return any(h in request.headers for h in CLOUDFLARE_HEADERS)


def check_key(request: Request) -> None:
    """The LAN door: lan_key, as always. (internet_key only works signed, on the internet door.)"""
    sent = request.headers.get("authorization", "").encode("latin-1", "replace")
    if KEYS["lan"] and not hmac.compare_digest(sent, f"Bearer {KEYS['lan']}".encode("utf-8")):
        raise HTTPException(401, "Wrong or missing API key")

# ===================== the internet door's rules =====================
class Refused(HTTPException):
    pass


PUBLIC_REASON = {400: "Request not allowed", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                 413: "Request too large", 415: "Only JSON is accepted", 429: "Too many requests"}
_log: dict[str, list] = {}                 # kind -> [last printed, lines skipped since]


def note(text: str, kind: str = "refused") -> None:
    """One console line per kind, at most every 5 seconds (so a flood can't fill the screen)."""
    now = time.monotonic()
    last = _log.setdefault(kind, [0.0, 0])
    if now - last[0] < 5:
        last[1] += 1
        return
    more = f" (+{last[1]} similar)" if last[1] else ""
    last[:] = [now, 0]
    print(f"[internet door] {time.strftime('%H:%M:%S')} {text}{more}")


def refuse(status: int, reason: str) -> Refused:
    note(f"refused: {reason}")
    return Refused(status, PUBLIC_REASON[status])   # the caller only learns the status


def address(request: Request) -> str:
    """Who is calling (Cloudflare sets cf-connecting-ip itself). IPv6 is grouped by /64."""
    raw = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "")
    try:
        ip = ipaddress.ip_address(raw.strip())
    except ValueError:
        return "unknown"
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


bad_keys: dict[str, deque] = {}             # address -> times of its recent wrong keys
nonces: dict[str, float] = {}               # one-time values already used -> forget after (monotonic)
blocked: dict[str, float] = {}              # address -> blocked until (monotonic)
recent: deque = deque()                     # times of accepted requests (last minute)
internet_inflight = 0


def remember_bad_key(who: str, now: float) -> None:
    if len(bad_keys) > 10_000:              # never let the tables grow without limit
        for k in [k for k, v in bad_keys.items() if not v or now - v[-1] > 600]:
            del bad_keys[k]
        if len(bad_keys) > 10_000:
            bad_keys.clear()
    times = bad_keys.setdefault(who, deque(maxlen=INTERNET_BAD_KEYS))
    times.append(now)
    if len(times) >= INTERNET_BAD_KEYS and now - times[0] <= 600:
        if len(blocked) > 10_000:
            for k in [k for k, until in blocked.items() if until <= now]:
                del blocked[k]
        blocked[who] = now + INTERNET_BLOCK_SECONDS
        bad_keys.pop(who, None)
        note(f"blocked an address for {INTERNET_BLOCK_SECONDS // 60} minutes after {INTERNET_BAD_KEYS} wrong keys", "blocked")


async def read_body(request: Request) -> bytes:
    """Read at most INTERNET_MAX_BODY bytes - never more, whatever the caller claims."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > INTERNET_MAX_BODY):
        raise refuse(413, "body too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > INTERNET_MAX_BODY:
            raise refuse(413, "body too large")
        chunks.append(chunk)
    return b"".join(chunks)


MODEL_RE = re.compile(r"[A-Za-z0-9_.:/@+-]{1,100}")
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
ROLES = {"system", "user", "assistant"}
FIELDS = {"model", "messages", "temperature", "top_p", "max_tokens", "stream", "response_format"}
SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
SCHEMA_KEYS = {"type", "properties", "required", "additionalProperties", "items", "enum",
               "minItems", "maxItems", "minLength", "maxLength", "description"}


def _number(value, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError("number out of range")
    return value


def clean_schema(node, depth: int = 0) -> dict:
    """A JSON schema for structured answers: plain keywords only (no $ref, patterns or nesting tricks)."""
    if depth > 8 or not isinstance(node, dict) or len(node) > 12:
        raise ValueError("schema too deep or not an object")
    out = {}
    for key, value in node.items():
        if key not in SCHEMA_KEYS:
            raise ValueError(f"schema keyword {key!r}")
        if key == "type":
            kinds = value if isinstance(value, list) else [value]
            if not kinds or len(kinds) > 7 or not all(isinstance(k, str) and k in SCHEMA_TYPES for k in kinds):
                raise ValueError("schema type")
        elif key == "properties":
            if not isinstance(value, dict) or len(value) > 40 or not all(isinstance(n, str) and NAME_RE.fullmatch(n) for n in value):
                raise ValueError("schema properties")
            value = {name: clean_schema(sub, depth + 1) for name, sub in value.items()}
        elif key == "items":
            value = clean_schema(value, depth + 1)
        elif key == "required":
            if not isinstance(value, list) or len(value) > 40 or not all(isinstance(n, str) and NAME_RE.fullmatch(n) for n in value):
                raise ValueError("schema required")
        elif key == "additionalProperties":
            if not isinstance(value, bool):
                raise ValueError("schema additionalProperties")
        elif key == "enum":
            if (not isinstance(value, list) or not 1 <= len(value) <= 64
                    or not all(v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200) for v in value)):
                raise ValueError("schema enum")
        elif key == "description":
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError("schema description")
        else:  # minItems / maxItems / minLength / maxLength
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
                raise ValueError(f"schema {key}")
        out[key] = value
    return out


def clean_chat(data) -> dict:
    """Rebuild a chat request from checked fields only. Anything unusual raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    extra = set(data) - FIELDS
    if extra:
        raise ValueError(f"field not allowed: {sorted(extra)[0][:40]!r}")
    messages = data.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 16:
        raise ValueError("messages")
    out: dict = {"messages": []}
    for m in messages:
        # text only: no images, audio, files, tool calls or names
        if not isinstance(m, dict) or set(m) - {"role", "content"} or m.get("role") not in ROLES \
                or not isinstance(m.get("content"), str):
            raise ValueError("messages must be plain text")
        out["messages"].append({"role": m["role"], "content": m["content"].replace("\x00", "")})
    if "model" in data:
        if not isinstance(data["model"], str) or not MODEL_RE.fullmatch(data["model"]):
            raise ValueError("model")
        out["model"] = data["model"]
    if "temperature" in data:
        out["temperature"] = _number(data["temperature"], 0, 2)
    if "top_p" in data:
        out["top_p"] = _number(data["top_p"], 0, 1)
    if "max_tokens" in data:
        mt = data["max_tokens"]
        if isinstance(mt, bool) or not isinstance(mt, int) or mt < 1:
            raise ValueError("max_tokens")
        out["max_tokens"] = min(mt, INTERNET_MAX_TOKENS)
    else:
        out["max_tokens"] = INTERNET_MAX_TOKENS
    if data.get("stream", False) is not False:
        raise ValueError("streaming is not offered here")
    out["stream"] = False
    if "response_format" in data:
        rf = data["response_format"]
        if not isinstance(rf, dict) or rf.get("type") not in ("json_schema", "json_object", "text"):
            raise ValueError("response_format")
        if rf["type"] == "json_schema":
            js = rf.get("json_schema")
            if set(rf) != {"type", "json_schema"} or not isinstance(js, dict) or set(js) - {"name", "strict", "schema"}:
                raise ValueError("response_format")
            if not isinstance(js.get("name", "answer"), str) or not NAME_RE.fullmatch(js.get("name", "answer")):
                raise ValueError("response_format name")
            if not isinstance(js.get("strict", True), bool):
                raise ValueError("response_format strict")
            schema = clean_schema(js.get("schema"))
            if len(json.dumps(schema)) > 32_000:
                raise ValueError("schema too large")
            out["response_format"] = {"type": "json_schema", "json_schema": {
                "name": js.get("name", "answer"), "strict": js.get("strict", True), "schema": schema}}
        elif set(rf) != {"type"}:
            raise ValueError("response_format")
        else:
            out["response_format"] = {"type": rf["type"]}
    return out


def _no_constants(name: str):
    raise ValueError(f"{name} is not allowed")


OFFERED = {("GET", "models"), ("POST", "chat/completions")}
HEX32 = re.compile(r"[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")


def signed(*parts: str) -> str:
    """HMAC-SHA256 with internet_key over the parts (the website computes the same)."""
    return hmac.new(KEYS["internet"].encode("utf-8"), "\n".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def forget_old_nonces(now: float) -> None:
    if len(nonces) > 1000:
        for k in [k for k, until in nonces.items() if until <= now]:
            del nonces[k]
        if len(nonces) > 20_000:            # only reachable with the key; stay bounded
            nonces.clear()


async def admit(request: Request, path: str) -> tuple[str, str, bytes, str]:
    """Every check for a request from the internet. Returns (method, path, clean body, nonce)."""
    now = time.monotonic()
    who = address(request)
    if blocked.get(who, 0) > now:
        raise refuse(403, "blocked address")
    route = (request.method, path)
    stamp = request.headers.get("x-gateway-time", "")
    nonce = request.headers.get("x-gateway-nonce", "")
    sig = request.headers.get("x-gateway-signature", "")
    if (route not in OFFERED or not KEYS["internet"] or not stamp.isdigit() or len(stamp) > 12
            or not HEX32.fullmatch(nonce) or not HEX64.fullmatch(sig)):
        remember_bad_key(who, now)          # unsigned, or a way in that isn't offered: same answer
        raise refuse(401, "unsigned request" if route in OFFERED else f"{request.method} /v1/{path[:60]} isn't offered")
    body = await read_body(request) if route == ("POST", "chat/completions") else b""
    want = signed("gw1", request.method, f"/v1/{path}", stamp, nonce, hashlib.sha256(body).hexdigest())
    if not hmac.compare_digest(want.encode(), sig.encode()):
        remember_bad_key(who, now)
        raise refuse(401, "wrong signature")
    skew = int(stamp) - int(time.time())
    if abs(skew) > INTERNET_CLOCK_WINDOW:  # a real signature, but too old (or this PC's clock is off)
        note(f"refused a signed request {skew:+d} s off - if that keeps happening, check this PC's clock "
             "(Settings > Time & language > Sync now)", "clock")
        raise Refused(401, PUBLIC_REASON[401])
    forget_old_nonces(now)
    if nonce in nonces:
        remember_bad_key(who, now)
        raise refuse(401, "replayed request")
    nonces[nonce] = now + 2 * INTERNET_CLOCK_WINDOW
    bad_keys.pop(who, None)
    while recent and now - recent[0] > 60:
        recent.popleft()
    if len(recent) >= INTERNET_PER_MINUTE or internet_inflight > INTERNET_MAX_INFLIGHT:  # counts itself
        raise refuse(429, "too many requests")
    if route == ("GET", "models"):
        recent.append(now)
        return "GET", "models", b"", nonce
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/json":
        raise refuse(415, f"content-type {kind[:40]!r}")
    if request.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise refuse(415, "compressed body")
    try:
        data = json.loads(body.decode("utf-8"), parse_constant=_no_constants)
        clean = clean_chat(data)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise refuse(400, str(exc)[:120] or "bad JSON") from None
    recent.append(now)
    return "POST", "chat/completions", json.dumps(clean, ensure_ascii=False).encode("utf-8"), nonce


def trimmed_reply(path: str, status: int, raw: bytes) -> tuple[int, dict]:
    """Only the answer goes back out - no model-server version, timings or file names."""
    if status != 200:
        note(f"model server answered HTTP {status}: {raw[:160].decode('utf-8', 'replace')!r}", "upstream")
        code = 502 if status in (401, 403) else status
        return code, {"error": {"message": "The model server could not answer this request.", "code": code}}
    try:
        data = json.loads(raw)
    except ValueError:
        return 502, {"error": {"message": "Bad reply from the model server.", "code": 502}}
    if not isinstance(data, dict):
        return 502, {"error": {"message": "Bad reply from the model server.", "code": 502}}
    if path == "models":
        ids = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
        safe = [i for i in ids if isinstance(i, str) and NAME_RE.fullmatch(i)][:8]
        return 200, {"object": "list", "data": [{"id": i, "object": "model"} for i in safe]}
    choices = []
    for c in data.get("choices") or []:
        if not isinstance(c, dict):
            continue
        msg = c.get("message") if isinstance(c.get("message"), dict) else {}
        content = msg.get("content")
        choices.append({"index": c.get("index", 0) if isinstance(c.get("index"), int) else 0,
                        "message": {"role": "assistant", "content": content if isinstance(content, str) else ""},
                        "finish_reason": c.get("finish_reason") if isinstance(c.get("finish_reason"), str) else None})
    out: dict = {"object": "chat.completion", "choices": choices}
    usage = data.get("usage")
    if isinstance(usage, dict):
        out["usage"] = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if isinstance(usage.get(k), int)}
    return 200, out
# =====================================================================

DROP_REQUEST_HEADERS = {"host", "content-length", "authorization", "connection", "transfer-encoding", "keep-alive"}
DROP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding", "keep-alive"}


@app.get("/status")
async def status(request: Request):
    if via_internet(request):
        raise HTTPException(404, "Not Found")  # LAN only: it lists your PCs' addresses
    return {
        "this_pc_cpu_percent": local_cpu,
        "backends": [
            {
                "name": b.name,
                "url": b.url,
                "online": b.healthy,
                "busy_slots": f"{b.inflight}/{b.max_inflight}",
                "overloaded": is_overloaded(b),
            }
            for b in backends
        ],
    }


async def open_upstream(method: str, path: str, *, headers: dict, body: bytes, params=None):
    """This PC first, then the other PC. Returns (open reply, backend); the caller closes the
    reply and gives the slot back (backend.inflight -= 1)."""
    tried: list[Backend] = []
    while True:
        b = choose(exclude=tried)
        if b is None:
            raise HTTPException(503, "No model server is available right now.")
        tried.append(b)
        b.inflight += 1
        try:
            upstream = await client.send(
                client.build_request(method, f"{b.url}/v1/{path}", params=params, headers=headers, content=body),
                stream=True,
            )
        except httpx.HTTPError:
            b.inflight -= 1
            b.healthy = False                  # couldn't reach it: try the other PC
            print(f"[gateway] {b.name} unreachable, switching")
            continue

        if upstream.status_code == 503 and choose(exclude=tried):
            await upstream.aclose()            # this one says it's busy/loading: try the other PC
            b.inflight -= 1
            continue
        return upstream, b


async def from_internet(path: str, request: Request) -> Response:
    global internet_inflight
    internet_inflight += 1
    try:
        method, path, body, nonce = await admit(request, path)
        # A fresh request: none of the caller's headers, only our own.
        headers = {"authorization": f"Bearer {KEYS['server']}"}
        if body:
            headers["content-type"] = "application/json"
        upstream, b = await open_upstream(method, path, headers=headers, body=body)
        try:
            raw = bytearray()
            async for chunk in upstream.aiter_bytes():
                raw += chunk
                if len(raw) > INTERNET_MAX_REPLY:
                    raise HTTPException(502, "Reply too large")
        finally:
            await upstream.aclose()
            b.inflight -= 1
    finally:
        internet_inflight -= 1
    status, payload = trimmed_reply(path, upstream.status_code, bytes(raw))
    out = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Signed back: the website only trusts an answer that this gateway signed for its own nonce.
    proof = signed("gw1-reply", nonce, str(status), hashlib.sha256(out).hexdigest())
    return Response(content=out, status_code=status, media_type="application/json",
                    headers={"x-gateway-reply": proof})


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    if via_internet(request):
        return await from_internet(path, request)

    check_key(request)  # your LAN: everything is passed through as before
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS}
    headers["authorization"] = f"Bearer {KEYS['server']}"
    upstream, b = await open_upstream(request.method, path, headers=headers, body=body,
                                      params=request.query_params)

    async def relay():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            b.inflight -= 1

    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS}
    out_headers["x-served-by"] = b.name
    return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)


class SecondServer(uvicorn.Server):
    """The internet door: the LAN server handles Ctrl+C and shuts both down (see stop_together)."""

    @contextlib.contextmanager
    def capture_signals(self):  # uvicorn >= 0.29
        yield

    def install_signal_handlers(self) -> None:  # older uvicorn
        pass


async def serve() -> None:
    # proxy_headers=False: a caller can't pretend to be someone else with X-Forwarded-For.
    lan = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=GATEWAY_PORT,
                                        proxy_headers=False, server_header=False))
    # The tunnel's door: this PC only. The health loop and HTTP client live in the LAN server.
    internet = SecondServer(uvicorn.Config(app, host="127.0.0.1", port=INTERNET_PORT, lifespan="off",
                                           proxy_headers=False, server_header=False,
                                           limit_concurrency=32, timeout_keep_alive=5))
    lan_task = asyncio.create_task(lan.serve())
    while not lan.started and not lan_task.done():
        await asyncio.sleep(0.1)
    if not lan.started:  # e.g. port 8000 already in use: uvicorn has printed why
        await lan_task
        return

    async def stop_together() -> None:  # Ctrl+C / closing reaches one server: stop both
        while not (lan.should_exit or internet.should_exit):
            await asyncio.sleep(0.5)
        lan.should_exit = internet.should_exit = True

    await asyncio.gather(lan_task, internet.serve(), stop_together())


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
