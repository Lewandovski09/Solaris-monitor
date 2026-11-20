# -*- coding: utf-8 -*-
"""
Solaris: облачный монитор станций Deye + графики отключений ДТЭК (Одесса).

Только ЧТЕНИЕ Deye Cloud: token, station/list, station/latest, station/history,
station/listWithDevice, device/latest. Никаких настроек инверторов программа не меняет.

Режимы:  python monitor.py          - обычная проверка (запускает расписание)
         python monitor.py test     - тестовое сообщение в Telegram
         python monitor.py diag     - что видит программа (уходит только сотрудникам)
         python monitor.py report   - отчёт за прошлый месяц сотрудникам и клиентам

Все пароли и токены берутся из переменных окружения (GitHub Secrets).
В журнал GitHub программа не пишет ни названий станций, ни адресов, ни данных.
"""
import glob, hashlib, json, os, re, secrets, subprocess, sys, time
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:  # pragma: no cover
    KYIV = timezone(timedelta(hours=2))

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("STATE_FILE") or os.path.join(HERE, "state.json")
BASE = {"EU": "https://eu1-developer.deyecloud.com/v1.0",
        "US": "https://us1-developer.deyecloud.com/v1.0"}
DTEK_URL = os.environ.get("DTEK_URL") or \
    "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/odesa.json"
OK_STATUS = ("NORMAL", "ONLINE", "1", "ALL_ONLINE")
EMERGENCY = re.compile(r"екстрен|аварійн|экстрен|аварийн", re.I)
EMERGENCY_NEG = re.compile(r"(уникн\w*|запобіг\w*|избежа\w*|без)\s+(\S+\s+){0,3}?(екстрен|аварійн|экстрен|аварийн)", re.I)


def env(k, d=""):
    return (os.environ.get(k) or d).strip()


def now():
    return datetime.now(KYIV)


def hm(dt):
    return dt.strftime("%H:%M")


def dur(minutes):
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    return f"{h} ч {m:02d} мин" if h else f"{m} мин"


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def kw(v, div=1000, nd=1):
    v = num(v)
    return "—" if v is None else f"{v / div:.{nd}f}"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- состояние
def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("stations", {})
    st.setdefault("log", [])
    st.setdefault("dtek", {})
    st.setdefault("api_fail", 0)
    return st


def save_state(st):
    cut = (now() - timedelta(days=100)).isoformat()
    st["log"] = [r for r in st["log"] if r.get("start", "") >= cut]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------------------------------------------------------- Telegram
class Telegram:
    def __init__(self):
        self.token = env("TELEGRAM_TOKEN")
        self.staff = env("TELEGRAM_CHAT_ID")
        self.sent = 0

    def send(self, chat, text):
        if not self.token or not chat:
            return False
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=20)
            self.sent += 1 if r.ok else 0
            if not r.ok:
                print("Telegram: сообщение не ушло, код", r.status_code)
            return r.ok
        except Exception:
            print("Telegram: ошибка сети")
            return False

    def staff_msg(self, text):
        return self.send(self.staff, text)

    def clients(self, chats, text):
        for chat in dict.fromkeys(str(c) for c in (chats or []) if c):
            if chat != str(self.staff):
                self.send(chat, text)

    def both(self, chats, text):
        """Сотрудникам всегда, клиентам - всем чатам, привязанным к станции."""
        self.staff_msg(text)
        self.clients(chats, text)

    def api(self, method, **params):
        r = requests.get(f"https://api.telegram.org/bot{self.token}/{method}", params=params, timeout=25)
        return r.json().get("result")


# ---------------------------------------------------------------- Deye Cloud
class Deye:
    def __init__(self):
        self.base = BASE.get(env("DEYE_DATA_CENTER", "EU").upper(), BASE["EU"])
        self.token = None

    def _post(self, path, body, auth=True, query=""):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "bearer " + self.token
        r = requests.post(self.base + path + query, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("success") is False or str(data.get("code", "1000000")) not in ("1000000", "0", "200"):
            raise RuntimeError(f"{path}: код {data.get('code')}")
        time.sleep(0.3)
        return data

    def login(self):
        pw = hashlib.sha256(env("DEYE_PASSWORD").encode("utf-8")).hexdigest()
        body = {"appSecret": env("DEYE_APP_SECRET"), "email": env("DEYE_EMAIL"), "password": pw}
        if env("DEYE_COMPANY_ID"):
            body["companyId"] = env("DEYE_COMPANY_ID")
        d = self._post("/account/token", body, auth=False, query="?appId=" + env("DEYE_APP_ID"))
        self.token = d.get("accessToken") or (d.get("data") or {}).get("accessToken")
        if not self.token:
            raise RuntimeError("токен не получен")

    def _pages(self, path, extra=None):
        out, page = [], 1
        while True:
            body = {"page": page, "size": 100}
            body.update(extra or {})
            d = self._post(path, body)
            items = d.get("stationList") or d.get("list") or []
            out += items
            if not items or len(out) >= (d.get("total") or len(out)) or page > 50:
                return out
            page += 1

    def stations(self):
        return self._pages("/station/list")

    def stations_with_devices(self):
        """Станции + серийные номера инверторов. Пробуем два способа, которые есть в OpenAPI."""
        self.dev_note = ""
        try:
            return self._pages("/station/listWithDevice", {"deviceType": "INVERTER"})
        except Exception as e:
            self.dev_note = "listWithDevice: " + str(e)[-40:]
        sts = self._pages("/station/list")
        ids = [s.get("id") or s.get("stationId") for s in sts]
        try:
            by = {}
            for i in range(0, len(ids), 10):
                page = 1
                while page < 20:
                    d = self._post("/station/device", {"page": page, "size": 100, "stationIds": ids[i:i + 10]})
                    items = d.get("deviceListItems") or d.get("list") or []
                    for it in items:
                        by.setdefault(str(it.get("stationId")), []).append(it)
                    if len(items) < 100:
                        break
                    page += 1
            for s in sts:
                s["deviceListItems"] = by.get(str(s.get("id") or s.get("stationId")), [])
            self.dev_note += " | station/device: ok"
        except Exception as e:
            self.dev_note += " | station/device: " + str(e)[-40:]
        return sts

    def latest(self, sid):
        return self._post("/station/latest", {"stationId": sid})

    def device_latest(self, sns):
        out = {}
        for i in range(0, len(sns), 10):
            d = self._post("/device/latest", {"deviceList": sns[i:i + 10]})
            for it in d.get("deviceDataList") or []:
                out[str(it.get("deviceSn"))] = {x.get("key"): x.get("value") for x in it.get("dataList") or []}
        return out

    def history(self, sid, gran, start, end):
        d = self._post("/station/history", {"stationId": sid, "granularity": gran, "startAt": start, "endAt": end})
        return d.get("stationDataItems") or d.get("dataList") or []


# ---------------------------------------------------------------- ДТЭК
SLOT_OFF = {"no": (1, 1), "first": (1, 0), "second": (0, 1)}
SLOT_MAYBE = {"maybe": (1, 1), "mfirst": (1, 0), "msecond": (0, 1)}


def queue_key(q):
    """'3.1', 'черга 3.1', 'GPV3.1' -> 'GPV3.1'"""
    m = re.search(r"(\d)\s*[.,]\s*(\d)", str(q or ""))
    return f"GPV{m.group(1)}.{m.group(2)}" if m else None


def slots(day):
    """Суточный график ДТЭК -> 48 получасовых отметок: 2 нет света, 1 возможно, 0 есть."""
    out = []
    for h in range(1, 25):
        v = str((day or {}).get(str(h), "yes"))
        a = SLOT_OFF.get(v)
        b = SLOT_MAYBE.get(v)
        out += [2 * a[0], 2 * a[1]] if a else ([b[0], b[1]] if b else [0, 0])
    return out


def intervals(sl, level=2):
    res, start = [], None
    for i, v in enumerate(sl + [None]):
        on = v == level
        if on and start is None:
            start = i
        if not on and start is not None:
            res.append((start, i))
            start = None
    return res


def slot_time(i):
    return f"{i // 2:02d}:{'30' if i % 2 else '00'}"


def fmt_intervals(sl):
    off = ", ".join(f"{slot_time(a)}–{slot_time(b)}" for a, b in intervals(sl, 2))
    may = ", ".join(f"{slot_time(a)}–{slot_time(b)}" for a, b in intervals(sl, 1))
    hours = sum(1 for v in sl if v == 2) / 2
    txt = f"без света: {off} (всего {hours:g} ч)" if off else "отключений по графику нет"
    if may:
        txt += f"\nвозможны отключения: {may}"
    return txt


def fetch_dtek():
    r = requests.get(DTEK_URL, timeout=25)
    r.raise_for_status()
    d = r.json()
    days = {}
    for ts, per_queue in ((d.get("fact") or {}).get("data") or {}).items():
        try:
            day = datetime.fromtimestamp(int(ts), KYIV).strftime("%Y-%m-%d")
        except Exception:
            continue
        days[day] = {q: slots(v) for q, v in (per_queue or {}).items()}
    return {"days": days, "attention": ((d.get("attention") or {}).get("text") or "").strip(),
            "updated": (d.get("fact") or {}).get("update") or "", "at": d.get("lastUpdated") or ""}


def planned_now(dtek, q, t, margin_min=40):
    """Идёт ли сейчас (с запасом по времени) плановое отключение для очереди."""
    if not dtek or not q:
        return None
    for delta in (0, -margin_min, margin_min):
        tt = t + timedelta(minutes=delta)
        sl = (dtek["days"].get(tt.strftime("%Y-%m-%d")) or {}).get(q)
        if sl is None:
            continue
        if sl[tt.hour * 2 + (1 if tt.minute >= 30 else 0)] == 2:
            return True
    return False if (dtek["days"].get(t.strftime("%Y-%m-%d")) or {}).get(q) is not None else None


def is_emergency(text):
    t = EMERGENCY_NEG.sub("", text or "")
    return bool(EMERGENCY.search(t))


# ---------------------------------------------------------------- настройки станций
def station_cfg():
    """STATIONS_JSON: {"Название или id станции": {"queue": "3.1", "client_chat": "-100..."}}"""
    try:
        raw = json.loads(env("STATIONS_JSON") or "{}")
    except Exception:
        print("STATIONS_JSON: не читается, проверьте скобки и кавычки")
        raw = {}
    return {str(k).strip().lower(): v for k, v in raw.items() if isinstance(v, dict)}


def cfg_for(cfgs, sid, name):
    return cfgs.get(str(sid).lower()) or cfgs.get(str(name).strip().lower()) or {}


def setup(st, cfgs, sid):
    """Настройки станции: из команд в чате сотрудников (память) и из STATIONS_JSON."""
    p = st["stations"].get(sid, {})
    c = cfg_for(cfgs, sid, p.get("name", ""))
    chats = list(p.get("clients") or [])
    if c.get("client_chat"):
        chats.append(str(c["client_chat"]))
    return {"title": p.get("title") or c.get("title") or p.get("name") or sid,
            "queue": queue_key(p.get("queue") or c.get("queue")),
            "chats": chats}


def find_station(st, text):
    """Станция по id или по части названия. Возвращает (sid, None) или (None, сообщение)."""
    t = str(text or "").strip().lower()
    if not t:
        return None, "Не указана станция."
    hits = [sid for sid, p in st["stations"].items()
            if t == sid or t in str(p.get("name", "")).lower() or t in str(p.get("title", "")).lower()]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, "Станция «%s» не найдена. Список: /stations" % esc(text)
    return None, "Подходит несколько станций: " + ", ".join(esc(st["stations"][h].get("name")) for h in hits)


HELP = ("<b>Команды (работают только в этом чате):</b>\n"
        "/link Название — ссылка-приглашение для клиента одной станции\n"
        "/stations Название — очередь, клиенты и сеть по одной станции\n"
        "/links и /stations без названия — список по всем станциям\n"
        "/queue Название 3.1 — указать очередь ДТЭК\n"
        "/title Название = Как называть в сообщениях\n"
        "/reset Название — отключить всех клиентов станции и выдать новую ссылку")


INBOX = os.path.join(HERE, "inbox")


def hook_secret():
    return hashlib.sha256(("tg:" + env("INBOX_KEY")).encode()).hexdigest()


def read_inbox():
    """Команды, которые приёмник (Vercel) положил в папку inbox зашифрованными."""
    out, key = [], env("INBOX_KEY")
    for path in sorted(glob.glob(os.path.join(INBOX, "*.txt"))):
        try:
            iv, body = open(path, encoding="utf-8").read().split("\n", 1)
            r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-base64", "-A",
                                "-K", hashlib.sha256(key.encode()).hexdigest(), "-iv", iv.strip()],
                               input=body.replace("\n", "").encode(), capture_output=True, timeout=20)
            if r.returncode == 0:
                out.append(json.loads(r.stdout.decode("utf-8")))
        except Exception:
            print("inbox: файл не прочитан")
        os.remove(path)
    return out


def commands(tg, st):
    """Читает сообщения боту: /start <код> от клиентов и команды из чата сотрудников."""
    if not tg.token:
        return
    T = st.setdefault("tg", {})
    ups = read_inbox() if env("INBOX_KEY") else []
    if ups:
        open(STATE + ".changed", "w").close()
    try:
        if not T.get("webhook"):
            ups += tg.api("getUpdates", offset=T.get("offset", 0), timeout=0,
                          allowed_updates=json.dumps(["message"])) or []
        if "bot" not in T:
            T["bot"] = (tg.api("getMe") or {}).get("username", "")
    except Exception:
        print("Telegram: команды не прочитаны")
    done = set(T.get("done") or [])
    for u in sorted(ups, key=lambda x: x.get("update_id", 0)):
        if u.get("update_id") in done:
            continue
        done.add(u.get("update_id"))
        T["done"] = sorted(done)[-200:]
        T["offset"] = max(T.get("offset", 0), u["update_id"] + 1)
        m = u.get("message") or {}
        chat = str((m.get("chat") or {}).get("id", ""))
        text = (m.get("text") or "").strip()
        if not chat or not text.startswith("/"):
            continue
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        who = esc(" ".join(x for x in ((m.get("from") or {}).get("first_name"), (m.get("from") or {}).get("last_name")) if x) or "клиент")
        if chat == str(tg.staff):
            staff_command(tg, st, cmd, arg)
        elif cmd == "/start":
            sid = next((k for k, p in st["stations"].items() if arg and p.get("code") == arg), None)
            if not sid:
                tg.send(chat, "Здравствуйте! Это бот уведомлений Solaris. Чтобы подключиться, откройте ссылку, которую вам прислал менеджер.")
                continue
            p = st["stations"][sid]
            if chat not in (p.get("clients") or []):
                p.setdefault("clients", []).append(chat)
            title = esc(p.get("title") or p.get("name"))
            ask = "" if queue_key(p.get("queue")) else ("\n\nЧтобы приходил график отключений, напишите свою очередь ДТЭК, например: /queue 3.1"
                                                        "\nОчередь указана в приложении и на сайте ДТЭК по вашему адресу.")
            tg.send(chat, f"✅ Подключено: <b>{title}</b>.\nСюда будут приходить сообщения, когда на объекте отключают и включают свет, графики ДТЭК и отчёт за месяц.\nОтключить уведомления: /stop{ask}")
            tg.staff_msg(f"👤 К станции <b>{title}</b> подключился клиент: {who}.")
        elif cmd == "/queue":
            mine = [k for k, p in st["stations"].items() if chat in (p.get("clients") or [])]
            m = re.fullmatch(r"\s*([1-6])\s*[.,]\s*([12])\s*", arg)
            if not mine:
                tg.send(chat, "Сначала подключитесь по ссылке от менеджера.")
            elif not m:
                tg.send(chat, "Напишите очередь так: /queue 3.1\nОчереди бывают от 1.1 до 6.2. Если не знаете свою — напишите менеджеру адрес объекта.")
            elif len(mine) > 1:
                tg.send(chat, "У вас несколько объектов. Напишите менеджеру, какая очередь у какого объекта.")
                tg.staff_msg(f"👤 Клиент {who} (несколько объектов) сообщил очередь ДТЭК {m.group(1)}.{m.group(2)} — уточните, для какого объекта, и задайте командой /queue.")
            else:
                p = st["stations"][mine[0]]
                p["queue"] = f"GPV{m.group(1)}.{m.group(2)}"
                tg.send(chat, f"✅ Очередь ДТЭК {m.group(1)}.{m.group(2)} записана. График отключений придёт в течение 10 минут.")
                tg.staff_msg(f"👤 Клиент {who} указал очередь ДТЭК {m.group(1)}.{m.group(2)} для <b>{esc(p.get('title') or p.get('name'))}</b>. Изменить: /queue {esc(p.get('name'))} X.Y")
        elif cmd == "/stop":
            n = 0
            for p in st["stations"].values():
                if chat in (p.get("clients") or []):
                    p["clients"].remove(chat)
                    n += 1
            tg.send(chat, "Уведомления отключены." if n else "Вы не были подключены.")
            if n:
                tg.staff_msg(f"👤 Клиент {who} отключил уведомления.")


def staff_command(tg, st, cmd, arg):
    S, bot = st["stations"], st.get("tg", {}).get("bot", "")
    if not S and cmd != "/help":
        tg.staff_msg("Станции ещё не загружены, повторите команду через 10 минут.")
    elif cmd == "/stations" and arg:
        sid, err = find_station(st, arg)
        if err:
            tg.staff_msg(err)
            return
        p = S[sid]
        tg.staff_msg(f"<b>{esc(p.get('name'))}</b>" + (f" → «{esc(p['title'])}»" if p.get("title") else "") +
                     f"\nочередь ДТЭК: {(queue_key(p.get('queue')) or 'не указана').replace('GPV', '')} · клиентов: {len(p.get('clients') or [])}"
                     f"\nсеть: {'НЕТ с ' + p['off_since'][11:16] if p.get('no_grid') else 'есть'} · связь: {'нет' if p.get('offline') else 'есть'}")
    elif cmd == "/stations":
        tg.staff_msg("\n\n".join(
            f"<b>{esc(p.get('name'))}</b>" + (f" → «{esc(p['title'])}»" if p.get("title") else "") +
            f"\nочередь ДТЭК: {(queue_key(p.get('queue')) or 'не указана').replace('GPV', '')} · клиентов: {len(p.get('clients') or [])}"
            for p in S.values()))
    elif cmd in ("/link", "/links") and arg:
        sid, err = find_station(st, arg)
        if err:
            tg.staff_msg(err)
            return
        p = S[sid]
        tg.staff_msg(f"Ссылка для клиента, <b>{esc(p.get('title') or p.get('name'))}</b>:\nhttps://t.me/{bot}?start={p.get('code')}")
    elif cmd == "/link":
        tg.staff_msg("Напишите станцию, можно часть названия. Пример: /link пбс\nВсе ссылки сразу: /links")
    elif cmd == "/links":
        tg.staff_msg("Ссылки для клиентов (кто откроет и нажмёт Start — будет получать уведомления по станции):\n\n" + "\n\n".join(
            f"<b>{esc(p.get('title') or p.get('name'))}</b>\nhttps://t.me/{bot}?start={p.get('code')}" for p in S.values() if p.get("code")))
    elif cmd == "/queue":
        m = re.match(r"(.+?)\s+(\d\s*[.,]\s*\d)\s*$", arg)
        if not m:
            tg.staff_msg("Пример: /queue ПБС 3.1")
            return
        sid, err = find_station(st, m.group(1))
        if err:
            tg.staff_msg(err)
            return
        S[sid]["queue"] = queue_key(m.group(2))
        tg.staff_msg(f"✅ <b>{esc(S[sid].get('name'))}</b>: очередь ДТЭК {S[sid]['queue'].replace('GPV', '')}. График придёт при следующей проверке.")
    elif cmd == "/title":
        name, _, title = arg.partition("=")
        sid, err = find_station(st, name)
        if err or not title.strip():
            tg.staff_msg(err or "Пример: /title ПБС = Офис на Хуторской")
            return
        S[sid]["title"] = title.strip()[:60]
        tg.staff_msg(f"✅ <b>{esc(S[sid].get('name'))}</b> теперь в сообщениях называется «{esc(S[sid]['title'])}».")
    elif cmd == "/reset":
        sid, err = find_station(st, arg)
        if err:
            tg.staff_msg(err)
            return
        tg.clients(S[sid].get("clients"), "Уведомления по объекту отключены менеджером.")
        S[sid]["clients"], S[sid]["code"] = [], secrets.token_urlsafe(9)
        tg.staff_msg(f"✅ <b>{esc(S[sid].get('name'))}</b>: клиенты отключены. Новая ссылка:\nhttps://t.me/{bot}?start={S[sid]['code']}")
    elif cmd in ("/help", "/start"):
        tg.staff_msg(HELP)


# ---------------------------------------------------------------- сеть есть / нет
VOLT_KEY = re.compile(r"grid.*volt|volt.*grid", re.I)


def grid_state(lt, dev):
    """True - сети нет, False - сеть есть, None - не понять. Плюс способ определения."""
    volts = [num(v) for k, v in (dev or {}).items() if k and VOLT_KEY.search(k)]
    volts = [v for v in volts if v is not None]
    if volts:
        return max(volts) < 50, "voltage"
    load = num(lt.get("consumptionPower")) or 0
    grid = num(lt.get("wirePower", lt.get("gridPower")))
    if grid is None:
        return None, "none"
    if abs(grid) >= 30:
        return False, "power"
    return (True if load > 300 else None), "power"


# ---------------------------------------------------------------- проверка
def check(api, tg, st, dtek):
    cfgs = station_cfg()
    low_soc = float(env("LOW_SOC", "40"))
    stale = float(env("STALE_MIN", "40"))
    t = now()
    try:
        api.login()
        stations = api.stations_with_devices()
        st["api_fail"] = 0
    except Exception as e:
        st["api_fail"] = st.get("api_fail", 0) + 1
        if st["api_fail"] == 3:
            tg.staff_msg("⚠️ <b>Deye Cloud не отвечает</b> уже 3 проверки подряд. Станции временно не проверяются.")
        print("Deye Cloud: ошибка,", type(e).__name__)
        return 0

    sns = {}
    for s in stations:
        for d in s.get("deviceListItems") or []:
            if str(d.get("deviceType", "")).upper() == "INVERTER" and d.get("deviceSn"):
                sns.setdefault(str(s.get("id") or s.get("stationId")), []).append(str(d["deviceSn"]))
    devdata = {}
    try:
        flat = [x for v in sns.values() for x in v]
        if flat:
            devdata = api.device_latest(flat)
    except Exception:
        devdata = {}

    for s in stations:
        sid = str(s.get("id") or s.get("stationId"))
        name = s.get("name") or s.get("stationName") or sid
        p = st["stations"].setdefault(sid, {})
        p["name"] = name
        p.setdefault("code", secrets.token_urlsafe(9))
        cfg = setup(st, cfgs, sid)
        title, chat, q = esc(cfg["title"]), cfg["chats"], cfg["queue"]
        try:
            lt = api.latest(int(sid))
        except Exception:
            lt = {}
        dev = {}
        for sn in sns.get(sid, []):
            dev.update(devdata.get(sn) or {})
        status = str(s.get("connectionStatus") or "").upper()
        upd = num(lt.get("lastUpdateTime") or s.get("lastUpdateTime"))
        age = None
        if upd:
            age = (time.time() - (upd / 1000 if upd > 1e11 else upd)) / 60
        soc = num(lt.get("batterySOC"))
        load = num(lt.get("consumptionPower")) or 0

        offline = bool((status and status not in OK_STATUS) or (age is not None and age > stale))
        if offline and not p.get("offline"):
            why = f"нет данных {age:.0f} мин" if age and age > stale else "логгер не отвечает"
            extra = " Перед этим на объекте не было сети — возможно, сел АКБ или выключился роутер." if p.get("no_grid") else ""
            tg.staff_msg(f"🔴 <b>{title}</b>: станция не на связи ({why}).{extra}\nПроверьте интернет / Wi-Fi логгер или позвоните клиенту.")
        if p.get("offline") and not offline:
            tg.staff_msg(f"🟢 <b>{title}</b>: снова на связи.")
        p["offline"] = offline
        if offline:
            continue  # пока нет связи, про сеть и АКБ ничего утверждать нельзя

        no_grid, how = grid_state(lt, dev)
        p["how"] = how
        if no_grid is True:
            p["off_n"] = p.get("off_n", 0) + 1
            need = 1 if how == "voltage" else 2  # по мощности - только после 2 проверок подряд
            if not p.get("no_grid") and p["off_n"] >= need:
                p["no_grid"] = True
                p["off_since"] = t.isoformat()
                p["off_soc"] = soc
                plan = planned_now(dtek, q, t)
                p["off_planned"] = plan
                if plan is True:
                    kind = "по графику ДТЭК"
                elif plan is False:
                    kind = "⚡ <b>вне графика</b> — похоже на экстренное или аварийное отключение"
                else:
                    kind = "очередь ДТЭК для объекта не указана" if not q else "график ДТЭК сейчас недоступен"
                bat = f"\nАКБ {soc:.0f}%, нагрузка {load / 1000:.1f} кВт." if soc is not None else f"\nНагрузка {load / 1000:.1f} кВт."
                left = ""
                tg.both(chat, f"🟠 <b>{title}</b>: свет отключили в ~{hm(t)}.\n{kind}.{bat}{left}\nОбъект работает от станции.")
        elif no_grid is False:
            p["off_n"] = 0
            if p.get("no_grid"):
                start = datetime.fromisoformat(p["off_since"])
                mins = (t - start).total_seconds() / 60
                st["log"].append({"sid": sid, "name": name, "start": p["off_since"], "end": t.isoformat(),
                                  "min": round(mins), "planned": p.get("off_planned"),
                                  "soc_start": p.get("off_soc"), "soc_end": soc})
                bat = ""
                if soc is not None and p.get("off_soc") is not None:
                    bat = f"\nАКБ: было {p['off_soc']:.0f}%, осталось {soc:.0f}%."
                tg.both(chat, f"🟢 <b>{title}</b>: свет включили в ~{hm(t)}.\nБез сети: {dur(mins)}.{bat}")
                for k in ("no_grid", "off_since", "off_soc", "off_planned", "low_sent"):
                    p.pop(k, None)

        low = soc is not None and soc < low_soc
        if low and not p.get("low_soc"):
            if p.get("no_grid"):
                tg.both(chat, f"🔋 <b>{title}</b>: заряд АКБ {soc:.0f}%, света всё ещё нет.\nСтоит отключить мощные приборы (бойлер, кондиционер, плиту), чтобы хватило дольше.")
            else:
                tg.staff_msg(f"🔋 <b>{title}</b>: заряд АКБ {soc:.0f}% при наличии сети (порог {low_soc:.0f}%). Если отключат свет — резерва может не хватить.")
        p["low_soc"] = bool(low)
    return len(stations)


# ---------------------------------------------------------------- графики ДТЭК
def dtek_alerts(tg, st, dtek):
    if not dtek:
        return
    cfgs = station_cfg()
    D = st["dtek"]
    t = now()
    today, tomorrow = t.strftime("%Y-%m-%d"), (t + timedelta(days=1)).strftime("%Y-%m-%d")
    # кто на какой очереди
    by_q, all_chats = {}, []
    for sid in st["stations"]:
        c = setup(st, cfgs, sid)
        all_chats += c["chats"]
        if c["queue"]:
            by_q.setdefault(c["queue"], []).append((c["title"], c["chats"]))

    # 1. общий текст ДТЭК: экстренные / аварийные
    att = dtek["attention"]
    h = hashlib.sha1(att.encode("utf-8")).hexdigest()
    if h != D.get("att_hash"):
        emerg = is_emergency(att)
        if D.get("att_hash") is not None or emerg:
            if emerg:
                msg = "🚨 <b>ДТЭК Одесса: экстренные / аварийные отключения</b>\nГрафики сейчас могут не действовать, свет могут отключить без предупреждения.\n\n" + esc(att[:700])
                tg.staff_msg(msg)
                tg.clients(all_chats, msg)
            elif D.get("emerg"):
                tg.staff_msg("✅ <b>ДТЭК Одесса</b>: сообщение об экстренных отключениях снято.\n\n" + esc(att[:500]))
            else:
                tg.staff_msg("ℹ️ <b>ДТЭК Одесса</b>, новое сообщение:\n\n" + esc(att[:700]))
        D["att_hash"], D["emerg"] = h, emerg

    # 2. графики по очередям: новый / изменился
    seen = D.setdefault("sched", {})
    for day, label in ((today, "сегодня"), (tomorrow, "завтра")):
        for q, people in by_q.items():
            sl = (dtek["days"].get(day) or {}).get(q)
            if sl is None:
                continue
            sig = "".join(map(str, sl))
            key = f"{day}|{q}"
            if seen.get(key) == sig:
                continue
            changed = key in seen
            seen[key] = sig
            head = "изменился график" if changed else "график"
            qn = q.replace("GPV", "")
            body = f"очередь {qn}, {label} ({day[8:]}.{day[5:7]}):\n{fmt_intervals(sl)}"
            names = ", ".join(esc(n) for n, _ in people)
            tg.staff_msg(f"🗓 <b>ДТЭК: {head}</b>, {body}\nОбъекты: {names}")
            for n, chats in people:
                tg.clients(chats, f"🗓 <b>{esc(n)}</b>: {head} отключений ДТЭК, {body}")
    for k in [k for k in seen if k.split("|")[0] < today]:
        seen.pop(k)

    # 3. напоминание за ~30 минут до отключения
    warned = D.setdefault("warned", {})
    for q, people in by_q.items():
        sl = (dtek["days"].get(today) or {}).get(q)
        if sl is None:
            continue
        for a, b in intervals(sl, 2):
            start = t.replace(hour=a // 2, minute=30 if a % 2 else 0, second=0, microsecond=0)
            left = (start - t).total_seconds() / 60
            key = f"{today}|{q}|{a}"
            if 0 < left <= 35 and key not in warned:
                warned[key] = 1
                for n, chat in people:
                    txt = f"⏰ <b>{esc(n)}</b>: по графику ДТЭК свет отключат в {slot_time(a)} (через ~{left:.0f} мин), до {slot_time(b)}."
                    tg.both(chat, txt)
    for k in [k for k in warned if k.split("|")[0] < today]:
        warned.pop(k)
    D["started"] = True


# ---------------------------------------------------------------- отчёт за месяц
MONTHS = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def report(api, tg, st):
    cfgs = station_cfg()
    t = now()
    y, m = (t.year, t.month - 1) if t.month > 1 else (t.year - 1, 12)
    ym = f"{y:04d}-{m:02d}"
    api.login()
    n = 0
    for s in api.stations():
        sid = str(s.get("id") or s.get("stationId"))
        name = s.get("name") or s.get("stationName") or sid
        st["stations"].setdefault(sid, {}).setdefault("name", name)
        c = setup(st, cfgs, sid)
        title = esc(c["title"])
        lines = [f"📊 <b>{title}</b> — отчёт за {MONTHS[m]} {y}"]
        try:
            rows = [r for r in api.history(int(sid), 3, ym, ym) if int(num(r.get("month")) or m) == m] or []
            r = rows[0] if rows else {}
            g, cns, buy = num(r.get("generationValue")), num(r.get("consumptionValue")), num(r.get("purchaseValue"))
            if g is not None:
                lines.append(f"☀️ Панели выработали: {g:,.0f} кВт·ч".replace(",", " "))
            if cns is not None:
                lines.append(f"🏠 Объект потребил: {cns:,.0f} кВт·ч".replace(",", " "))
            if buy is not None:
                lines.append(f"🔌 Куплено из сети: {buy:,.0f} кВт·ч".replace(",", " "))
            if cns and buy is not None:
                lines.append(f"✅ Своей энергией покрыто: {max(0, (1 - buy / cns) * 100):.0f}%")
        except Exception:
            lines.append("Данные по выработке за месяц Deye Cloud не отдал.")
        out = [r for r in st["log"] if r.get("sid") == sid and r.get("start", "")[:7] == ym]
        if out:
            total = sum(r["min"] for r in out)
            unpl = [r for r in out if r.get("planned") is False]
            lines.append(f"\n💡 Отключений света: {len(out)}, всего {dur(total)}.")
            lines.append(f"Самое долгое: {dur(max(r['min'] for r in out))}.")
            if unpl:
                lines.append(f"Вне графика (экстренные/аварийные): {len(unpl)}, {dur(sum(r['min'] for r in unpl))}.")
            lines.append("Всё это время объект работал от станции.")
        else:
            lines.append("\n💡 Отключений света за месяц программа не зафиксировала.")
        tg.both(c["chats"], "\n".join(lines))
        n += 1
    return n


def diag(api, tg, st, dtek):
    cfgs = station_cfg()
    api.login()
    stations = api.stations_with_devices()
    mode = api.dev_note or "listWithDevice: ok"
    keys, types = set(), set()
    lines = [f"🔧 <b>Диагностика</b>: станций {len(stations)}"]
    for s in stations[:25]:
        sid = str(s.get("id") or s.get("stationId"))
        name = s.get("name") or s.get("stationName") or sid
        st["stations"].setdefault(sid, {}).setdefault("name", name)
        c = setup(st, cfgs, sid)
        types |= {str(d.get("deviceType")) for d in s.get("deviceListItems") or []}
        sn = [str(d.get("deviceSn")) for d in s.get("deviceListItems") or [] if str(d.get("deviceType", "")).upper() == "INVERTER"]
        dev = {}
        try:
            for v in (api.device_latest(sn) if sn else {}).values():
                dev.update(v)
        except Exception as e:
            mode += " | device/latest: " + str(e)[-40:]
        keys |= set(k for k in dev if k)
        try:
            lt = api.latest(int(sid))
        except Exception:
            lt = {}
        ng, how = grid_state(lt, dev)
        volts = {k: v for k, v in dev.items() if k and VOLT_KEY.search(k)}
        lines.append(f"\n<b>{esc(name)}</b> (id {sid})\nсеть: {'НЕТ' if ng else 'есть' if ng is False else 'неясно'} · способ: {how}"
                     f"\nнапряжение сети: {', '.join(str(v) for k, v in sorted(volts.items()) if 'L1L2' not in k or len(volts) == 1) + ' В' if volts else 'нет в данных'}"
                     f"\nАКБ {kw(lt.get('batterySOC'), 1, 0)}% · нагрузка {kw(lt.get('consumptionPower'))} кВт · из сети {kw(lt.get('wirePower', lt.get('gridPower')))} кВт"
                     f"\nочередь ДТЭК: {(c['queue'] or 'не указана').replace('GPV', '')} · клиентов подключено: {len(c['chats'])}")
    if dtek:
        lines.append(f"\nДТЭК: график обновлён {esc(dtek['updated'])}, дни: {', '.join(sorted(dtek['days']))}")
    else:
        lines.append("\nДТЭК: график сейчас недоступен")
    tg.staff_msg("\n".join(lines))
    # в журнал - только технические названия полей, без станций и значений
    print("DIAG mode:", mode)
    print("DIAG device types:", sorted(types))
    print("DIAG device keys:", sorted(keys))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    tg = Telegram()
    st = load_state()
    before = json.dumps(st, sort_keys=True, ensure_ascii=False)
    if mode == "test":
        ok = tg.staff_msg("✅ Монитор Solaris подключён. Сюда будут приходить уведомления о станциях и графиках ДТЭК.")
        print("Тест:", "ушло" if ok else "НЕ ушло — проверьте TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")
        sys.exit(0 if ok else 1)
    try:
        dtek = fetch_dtek()
        st["dtek"]["fail"] = 0
    except Exception:
        dtek = None
        st["dtek"]["fail"] = st["dtek"].get("fail", 0) + 1
        if st["dtek"]["fail"] == 12:
            tg.staff_msg("⚠️ Графики ДТЭК не загружаются уже час. Отключения по станциям по-прежнему отслеживаются.")
    api = Deye()
    if mode == "cmd":
        commands(tg, st)
        print("Команды обработаны, сообщений:", tg.sent)
    elif mode in ("webhook", "webhook-off"):
        url = ""
        try:
            url = open(os.path.join(HERE, "webhook_url.txt"), encoding="utf-8").read().strip()
        except Exception:
            pass
        if mode == "webhook" and url and env("INBOX_KEY"):
            ok = tg.api("setWebhook", url=url, secret_token=hook_secret(),
                        allowed_updates=json.dumps(["message"]))
            st.setdefault("tg", {})["webhook"] = bool(ok)
            tg.staff_msg("⚡ Быстрые команды включены: бот отвечает примерно за минуту." if ok else "⚠️ Быстрые команды включить не получилось.")
        else:
            tg.api("deleteWebhook")
            st.setdefault("tg", {})["webhook"] = False
            tg.staff_msg("Быстрые команды выключены: бот снова отвечает при очередной проверке.")
        print("webhook:", st["tg"]["webhook"])
    elif mode == "diag":
        try:
            diag(api, tg, st, dtek)
        except Exception as e:
            tg.staff_msg("⚠️ Диагностика: не получилось войти в Deye Cloud. Проверьте секреты DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL, DEYE_PASSWORD.")
            print("Диагностика: ошибка,", type(e).__name__)
    elif mode == "report":
        try:
            print("Отчётов:", report(api, tg, st))
        except Exception as e:
            tg.staff_msg("⚠️ Отчёт за месяц не собрался: Deye Cloud не ответил. Запустите его вручную позже (Actions → monitor → report).")
            print("Отчёт: ошибка,", type(e).__name__)
    else:
        commands(tg, st)
        n = check(api, tg, st, dtek)
        dtek_alerts(tg, st, dtek)
        st["last_check"] = now().strftime("%Y-%m-%d %H:%M")
        print(f"Проверено станций: {n}, сообщений: {tg.sent}, ДТЭК: {'ок' if dtek else 'нет'}")
    # «изменилось ли состояние» - без учёта времени последней проверки
    a, b = json.loads(before), json.loads(json.dumps(st, sort_keys=True, ensure_ascii=False))
    a.pop("last_check", None); b.pop("last_check", None)
    save_state(st)
    if a != b:
        open(STATE + ".changed", "w").close()


if __name__ == "__main__":
    main()
