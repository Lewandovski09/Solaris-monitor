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


def since_hm(dt):
    """Час початку; якщо не сьогодні — з датою: «22.11 ~09:14»."""
    dt = dt.astimezone(KYIV)
    return f"~{hm(dt)}" if dt.date() == now().date() else f"{dt.strftime('%d.%m')} ~{hm(dt)}"


def dur(minutes):
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    return f"{h} год {m:02d} хв" if h else f"{m} хв"


def dur_ru(minutes):
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
        self.last_id = None
        self.blocked = set()

    def photo(self, chat, path, caption, markup=None):
        """Картинка с подписью. Если не получилось - обычный текст."""
        try:
            data = {"chat_id": chat, "caption": caption[:1000], "parse_mode": "HTML"}
            if markup:
                data["reply_markup"] = json.dumps(markup)
            with open(path, "rb") as f:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendPhoto", data=data, files={"photo": f}, timeout=30)
            if r.ok:
                self.sent += 1
                return True
        except Exception:
            pass
        return self.send(chat, caption, markup)

    def call(self, method, **params):
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params, timeout=20)
            return r.json() if r.content else {}
        except Exception:
            return {}

    def send(self, chat, text, markup=None):
        if not self.token or not chat:
            return False
        try:
            body = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
            if markup:
                body["reply_markup"] = markup
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=body, timeout=20)
            try:
                self.last_id = r.json().get("result", {}).get("message_id")
            except Exception:
                self.last_id = None
            self.sent += 1 if r.ok else 0
            if r.status_code == 403 and str(chat) != str(self.staff):
                self.blocked.add(str(chat))  # клиент заблокировал бота или удалил чат
            if not r.ok:
                print("Telegram: сообщение не ушло, код", r.status_code)
            return r.ok
        except Exception:
            print("Telegram: ошибка сети")
            return False

    def staff_msg(self, text):
        return self.send(self.staff, text)

    def quiet(self):
        """Тихие часы у клиентов: с 23:00 до 07:00 по Киеву."""
        h = now().hour
        return h >= 23 or h < 7

    def clients(self, chats, text, urgent=False, markup=None):
        """Клиентам. Ночью уходит только срочное (свет отключили / включили), остальное - в утренней сводке."""
        if not urgent and self.quiet():
            return False
        for chat in dict.fromkeys(str(c) for c in (chats or []) if c):
            if chat != str(self.staff):
                self.send(chat, text, markup)
        return True

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
        self.units = {}  # единицы измерения полей device/latest: {sn: {поле: единица}}

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
                self.units[str(it.get("deviceSn"))] = {x.get("key"): x.get("unit") for x in it.get("dataList") or []}
        return out

    def alerts(self, sid, start, end):
        d = self._post("/station/alertList", {"stationId": sid, "startTimestamp": int(start), "endTimestamp": int(end), "page": 1, "size": 50})
        return d.get("stationAlertItems") or []

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
    txt = f"без світла: {off} (разом {hours:g} год)" if off else "відключень за графіком немає"
    if may:
        txt += f"\nможливі відключення: {may}"
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


def planned_end(dtek, q, t):
    """Когда по графику очереди закончится текущее отключение."""
    if not dtek or not q:
        return None
    cur = t.replace(minute=30 if t.minute >= 30 else 0, second=0, microsecond=0)
    for _ in range(96):
        sl = (dtek["days"].get(cur.strftime("%Y-%m-%d")) or {}).get(q)
        if sl is None:
            return None if cur.date() == t.date() else cur  # график на завтра ещё не вышел - считаем до полуночи
        if sl[cur.hour * 2 + (1 if cur.minute >= 30 else 0)] != 2:
            return cur if cur > t else None
        cur += timedelta(minutes=30)
    return None


# ---------------------------------------------------------------- две линии и проверка очереди
ALL_Q = [f"GPV{a}.{b}" for a in range(1, 7) for b in (1, 2)]
MODE_RU = {"q1": "станция на линии 1", "q2": "станция на линии 2", "ats": "АВР: света нет, только когда отключены обе линии"}


def queues(p):
    """Очереди объекта: основная и, если есть, вторая линия."""
    return [q for q in (queue_key(p.get("queue")), queue_key(p.get("queue2"))) if q]


def sched_on(p, chat):
    """Клиент не отключал графики по этому объекту."""
    return str(chat) not in (p.get("nosched") or [])


def eff_key(dtek, sid, p, q):
    """Очередь, по которой судить «по графику или нет». Для двух линий строим общий график:
    АВР - света нет, только когда нет обеих линий; пока схема не определена - когда нет хотя бы одной."""
    q2 = queue_key(p.get("queue2"))
    if not dtek or not q or not q2 or q2 == q:
        return q
    mode = p.get("lines_mode") or "auto"
    if mode == "q2":
        return q2
    if mode == "q1":
        return q
    key = f"EFF{sid}"
    for day in dtek["days"].values():
        a, b = day.get(q), day.get(q2)
        if a is not None and b is not None:
            day[key] = [min(x, y) if mode == "ats" else max(x, y) for x, y in zip(a, b)]
    return key


def snapshot(dtek, t):
    """График всех очередей на сегодня и завтра в момент отключения: ДТЭК потом может его поменять."""
    if not dtek:
        return None
    out = {}
    for d in (t, t + timedelta(days=1)):
        day = dtek["days"].get(d.strftime("%Y-%m-%d"))
        if day:
            out[d.strftime("%Y-%m-%d")] = {q: "".join(map(str, day[q])) for q in ALL_Q if day.get(q) is not None}
    return out or None


def matches(snap, start, end, tol=40, pairs=()):
    """С какими очередями совпало отключение: и начало, и конец в пределах ±tol минут от интервала графика.
    Возвращает словарь {вариант: True/False}, где вариант - очередь или пара очередей «A+B» (АВР)."""
    d0 = start.strftime("%Y-%m-%d")
    d1 = (start + timedelta(days=1)).strftime("%Y-%m-%d")
    if not snap or d0 not in snap:
        return {}
    base = start.replace(hour=0, minute=0, second=0, microsecond=0)

    def line(q):
        a = snap[d0].get(q)
        if a is None:
            return None
        b = (snap.get(d1) or {}).get(q) or "0" * 48
        return [int(c) for c in a + b]

    def fits(sl):
        for a, b in intervals(sl, 2):
            s, e = base + timedelta(minutes=30 * a), base + timedelta(minutes=30 * b)
            if abs((start - s).total_seconds()) <= tol * 60 and abs((end - e).total_seconds()) <= tol * 60:
                return True
        return False

    res = {}
    for q in ALL_Q:
        sl = line(q)
        if sl is not None:
            res[q] = fits(sl)
    for qa, qb in pairs:
        a, b = line(qa), line(qb)
        if a is not None and b is not None:
            res[f"{qa}+{qb}"] = fits([min(x, y) for x, y in zip(a, b)])
    return res


def overlaps(a, b):
    return a.get("start", "") < b.get("end", "") and b.get("start", "") < a.get("end", "")


def queue_review(tg, st, sid, p, t):
    """После отключений: определить схему двух линий и заметить, что очередь, похоже, сменилась.
    Считаются только отключения, которые точно совпали с графиком какой-то очереди (начало и конец ±40 мин)
    и прошли не в день экстренных/аварийных отключений."""
    q1, q2 = queue_key(p.get("queue")), queue_key(p.get("queue2"))
    if not q1 or tg.quiet():
        return
    title = esc(p.get("title") or p.get("name"))
    recent = [x for x in st["log"] if x.get("sid") == sid and x.get("start", "") >= (t - timedelta(days=7)).isoformat()
              and x.get("m") and not x.get("emerg")]
    qn = lambda q: q.replace("GPV", "")

    # 1. две линии: какая схема
    if q2 and (p.get("lines_mode") or "auto") == "auto":
        votes = []
        for x in recent[-3:]:
            m = set(x["m"])
            opts = [k for k, ok in (("q1", q1 in m), ("q2", q2 in m), ("ats", f"{q1}+{q2}" in m or f"{q2}+{q1}" in m)) if ok]
            if len(opts) == 1:  # совпало ровно с одной схемой - это голос; если с несколькими, графики похожи и это ничего не говорит
                votes.append(opts[0])
        if len(votes) >= 2 and len(set(votes[-2:])) == 1:
            p["lines_mode"] = votes[-1]
            tg.staff_msg(f"🔌 <b>{esc(p.get('name'))}</b>: две линии, очереди {qn(q1)} и {qn(q2)}. Определено: {MODE_RU[votes[-1]]}.\n"
                         f"Как определено: последние отключения совпали по началу и концу (±40 мин) только с этой схемой: {len(votes)} из {len(recent[-3:])}. "
                         f"Экстренные дни не учитывались.\nЕсли неверно: <code>/lines {esc(p.get('name'))} ats</code> (или 1, 2, auto).")

    # 2. очередь, похоже, сменилась
    last3 = recent[-3:]
    if len(last3) < 3 or len({x["start"][:10] for x in last3}) < 3:
        return
    own = set(queues(p)) | ({f"{q1}+{q2}", f"{q2}+{q1}"} if q2 else set())
    if any(set(x["m"]) & own for x in last3):
        return  # хоть одно отключение по своей очереди - всё в порядке
    common = {c for c in set.intersection(*[set(x["m"]) for x in last3]) if "+" not in c}
    if not common:
        return
    # Подтверждение: если у нас есть другие станции на той очереди, хотя бы у одной в эти дни тоже было отключение.
    others = [k for k, pp in st["stations"].items() if k != sid and common & set(queues(pp))]
    if others and not any(o.get("sid") in others and overlaps(o, x) for x in last3 for o in st["log"]):
        return
    if p.get("qok_until", "") > t.isoformat() or (p.get("qsug_at") and (t - datetime.fromisoformat(p["qsug_at"])).days < 14):
        return
    p["qsug_at"] = t.isoformat()
    alt = sorted(common)[:3]
    alt_txt = " або ".join(qn(q) for q in alt)
    days = ", ".join(datetime.fromisoformat(x["start"]).strftime("%d.%m") for x in last3)
    kb = {"inline_keyboard": [[{"text": "Перевірити на сайті ДТЕК", "url": "https://www.dtek-oem.com.ua/ua/shutdowns"}]]
          + [[{"text": f"Змінити на {qn(q)}", "callback_data": f"cq:{sid}:{qn(q)}"}] for q in alt]
          + [[{"text": "Моя черга правильна", "callback_data": f"qk:{sid}"}]]}
    tg.clients([c for c in p.get("clients") or []], f"🔁 <b>{title}</b>: схоже, черга ДТЕК змінилась.\n"
               f"Останні 3 відключення ({days}) збіглися з графіком черги {alt_txt}, а не {qn(q1)}.\n"
               f"Перевірте свою чергу на сайті ДТЕК: введіть адресу, і під нею буде номер черги.", markup=kb)
    tg.staff_msg(f"🔁 <b>{esc(p.get('name'))}</b>: отключения 3 раза подряд ({days}) совпали с очередью {alt_txt}, а записана {qn(q1)}. Клиенту предложено проверить.\n"
                 f"Как посчитано: начало и конец каждого отключения совпали с графиком {alt_txt} (±40 мин, график на момент отключения) и не совпали с {qn(q1)}; "
                 f"дни экстренных отключений не учитывались" + ("; у другой станции на этой очереди в те же дни тоже не было света." if others else "."))


def usual_rate(st, sid):
    """Обычная скорость разряда без сети, % в час: медиана по отключениям за 30 дней."""
    cut = (now() - timedelta(days=30)).isoformat()
    r = sorted((x["soc_start"] - x["soc_end"]) / (x["min"] / 60) for x in st["log"]
               if x.get("sid") == sid and x.get("start", "") >= cut and x.get("min", 0) >= 45
               and x.get("soc_start") is not None and x.get("soc_end") is not None and x["soc_start"] > x["soc_end"])
    return r[len(r) // 2] if len(r) >= 2 else None


# ---------------------------------------------------------------- запас заряда АКБ
CAP_KEY = re.compile(r"batt?(ery)?.*capacit|capacit.*batt?|bms.*capacit", re.I)
BVOLT_KEY = re.compile(r"^(batt?(ery)?|bms)[_ ]?.*volt", re.I)


def deye_time(lt):
    """Время данных по часам Deye (а не время проверки бота)."""
    upd = num((lt or {}).get("lastUpdateTime"))
    if not upd:
        return None
    return datetime.fromtimestamp(upd / 1000 if upd > 1e11 else upd, KYIV)


def discharge_kw(lt):
    """Сколько кВт сейчас отдаёт АКБ. Берём мощность разряда из Deye, а если её нет —
    нагрузку минус солнце (+5% на потери инвертора). 0 — АКБ не разряжается (заряжается от солнца)."""
    lt = lt or {}
    d, c = num(lt.get("dischargePower")), num(lt.get("chargePower"))
    if d is not None and d > 0:
        return d / 1000
    if c is not None and c > 0:
        return 0.0
    load, pv = num(lt.get("consumptionPower")), num(lt.get("generationPower")) or 0
    if load is None:
        return None
    return max(0.0, (load - pv) / 1000 * 1.05)


def deye_cap_kwh(dev, units):
    """Ёмкость АКБ из данных инвертора в Deye, кВт·ч: (всего, только первый вход, подпись А·ч).
    У инверторов 50 кВт два входа АКБ (BatteryRatedCapacity и Battery2RatingCapacity, в А·ч):
    А·ч × напряжение своего входа. Второй вход учитывается, только если на нём есть напряжение."""
    dev, units = dev or {}, units or {}
    total, first, sig = 0.0, 0.0, []
    for k, v in sorted(dev.items()):
        val = num(v)
        if not k or not CAP_KEY.search(k) or not val or val <= 0:
            continue
        u = str(units.get(k) or "").lower().replace("·", "").replace(" ", "")
        second = bool(re.search(r"batt?(ery)?2|2$", k, re.I))
        kwh = None
        if u == "kwh":
            kwh = val
        elif u == "wh":
            kwh = val / 1000
        elif u == "ah":
            names = ("BatteryVoltage2", "BMS2Voltage") if second else ("BatteryVoltage", "BMSVoltage")
            volt = next((num(dev[n]) for n in names if (num(dev.get(n)) or 0) > 10), None)
            if volt is None and not second:
                volt = 51.2
            if volt:
                kwh = val * volt / 1000
        if kwh:
            total += kwh
            first += 0 if second else kwh
            sig.append(f"{k}={val:g}")
    return (total, first or total, ",".join(sig)) if total else (None, None, "")


def learned_cap(st, sid):
    """Фактическая ёмкость по прошлым отключениям: сколько кВт·ч отдала АКБ ÷ на сколько % упал заряд."""
    cut = (now() - timedelta(days=60)).isoformat()
    r = sorted(x["kwh"] / ((x["soc_start"] - x["soc_end"]) / 100) for x in st["log"]
               if x.get("sid") == sid and x.get("start", "") >= cut and x.get("kwh")
               and x.get("soc_start") is not None and x.get("soc_end") is not None and x["soc_start"] - x["soc_end"] >= 10)
    return r[len(r) // 2] if len(r) >= 2 else None


def capacity(st, sid, p):
    """Ёмкость АКБ, кВт·ч, и откуда она.
    1) вписана командой /akb - главная; 2) из Deye; 3) по факту отключений.
    Если факт отключений уже есть и расходится с Deye больше чем на 20%, для прогноза берём факт:
    в инвертор могли вписать неверные А·ч, оба входа 50 кВт могут идти на одну батарею, батарея стареет."""
    if p.get("cap_kwh"):
        return p["cap_kwh"], "вписана вручную"
    fact = learned_cap(st, sid)
    d = p.get("cap_deye")
    if d and fact and abs(fact - d) / d > 0.2:
        d1 = p.get("cap_deye1")
        if d1 and d1 != d and abs(fact - d1) / d1 <= 0.2:
            return d1, "из Deye, только первый вход АКБ (по факту отключений второй вход — та же батарея)"
        return fact, "по факту отключений (Deye показывает другую)"
    if d:
        return d, "из Deye"
    return (fact, "по прошлым отключениям") if fact else (None, "")


def cap_warn(tg, st, sid, p, t):
    """Ёмкость в настройках не совпадает с фактом больше чем на 20% - сообщить сотрудникам (раз в 14 дней)."""
    set_cap = p.get("cap_kwh") or p.get("cap_deye")
    fact = learned_cap(st, sid)
    if not set_cap or not fact or abs(fact - set_cap) / set_cap <= 0.2:
        return
    if p.get("capwarn") and (t - datetime.fromisoformat(p["capwarn"])).days < 14:
        return
    p["capwarn"] = t.isoformat()
    src = "вписана командой /akb" if p.get("cap_kwh") else "в настройках инвертора (Deye)"
    tg.staff_msg(f"⚙️ <b>{esc(p.get('name'))}</b>: ёмкость АКБ {src} — {set_cap:.0f} кВт·ч, а по факту отключений ~{fact:.0f} кВт·ч.\n"
                 f"Как посчитано: сколько кВт·ч отдала АКБ за отключение ÷ на сколько % упал заряд, медиана за 60 дней (отключения с падением от 10%).\n"
                 f"Прогнозы клиенту уже считаются по факту{' (кроме случая, когда ёмкость вписана вручную)' if p.get('cap_kwh') else ''}.\n"
                 f"Что сделать: проверить Battery Capacity в настройках инвертора и количество модулей АКБ. Если в Deye неверно — поправить там; "
                 f"если верно, а батарея отдаёт меньше — проверить состояние АКБ (SOH, баланс модулей).")


def usual_kw(st, sid):
    """Обычная мощность разряда АКБ при отключениях, кВт: медиана за 30 дней."""
    cut = (now() - timedelta(days=30)).isoformat()
    r = sorted(x["kwh"] / (x["min"] / 60) for x in st["log"]
               if x.get("sid") == sid and x.get("start", "") >= cut and x.get("kwh") and x.get("min", 0) >= 45)
    return r[len(r) // 2] if len(r) >= 2 else None


def runtime(st, sid, p, soc, dkw):
    """На сколько часов хватит заряда до 15%. Возвращает (часы, способ).
    Способы: energy - по ёмкости и текущей мощности разряда; trend - по падению % в этом отключении
    (только после часа и падения на 5%+); usual - по обычной скорости разряда этой станции.
    Если доступно несколько, берём самый осторожный (меньший). (None, "pv") - АКБ сейчас не разряжается."""
    if soc is None:
        return None, ""
    usable = max(0.0, soc - 15)
    est = []
    cap, _ = capacity(st, sid, p)
    if cap and dkw is not None:
        if dkw < 0.05:
            return None, "pv"
        est.append((usable / 100 * cap / dkw, "energy"))
    if p.get("no_grid") and p.get("off_soc") is not None and p.get("off_since"):
        hrs = (now() - datetime.fromisoformat(p["off_since"])).total_seconds() / 3600
        drop = p["off_soc"] - soc
        if hrs >= 1 and drop >= 5:
            est.append((usable / (drop / hrs), "trend"))
    if not est:
        rate = usual_rate(st, sid)
        if rate and rate > 0:
            est.append((usable / rate, "usual"))
    return min(est) if est else (None, "")


def forecast_line(st, sid, soc, t, until, p=None, dkw=None):
    """Строка «хватит ли заряда до конца отключения»."""
    out = f"\nЗа графіком світло мають дати о {hm(until)}." if until else ""
    left_h, how = runtime(st, sid, p or {}, soc, dkw)
    if left_h is None:
        return out
    word = "за поточного споживання" if how == "energy" else "за звичного споживання"
    empty = t + timedelta(hours=left_h)
    if until:
        if empty >= until + timedelta(minutes=30):
            out += f" Заряду має вистачити: {word} його стане до ~{hm(empty)}."
        else:
            out += f"\n⚠️ Заряду може не вистачити: {word} АКБ опуститься до 15% близько {hm(empty)}. Вимкніть потужні прилади."
    else:
        out += f"\n{word[0].upper() + word[1:]} заряду вистачить приблизно на {dur(left_h * 60)}."
    return out


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


HELP = ("📋 <b>Команды бота</b>\n"
        "Пишутся в этой группе. Ответ приходит примерно за минуту. <b>Название станции</b> пишите как в Deye Cloud, регистр букв не важен. Если не помните точно — напишите часть, бот покажет подходящие кнопками.\n\n"
        "<b>Дать клиенту доступ</b>\n"
        "<code>/link пбс</code> — бот пришлёт ссылку. Отправьте её клиенту, он нажимает Start и начинает получать сообщения о свете.\n\n"
        "<b>Посмотреть станцию</b>\n"
        "<code>/stations пбс</code> — очередь ДТЭК, сколько клиентов подключено, есть ли сейчас свет и связь.\n"
        "<code>/stations</code> — сводка: где сейчас нет света, кто не на связи, у кого низкий заряд.\n\n"
        "<b>Указать очередь ДТЭК</b>\n"
        "<code>/queue пбс 3.1</code> — после этого клиенту идут графики и напоминания. Обычно клиент присылает очередь сам.\n"
        "<code>/queue пбс 3.1 5.2</code> — на объекте две линии. Схему (АВР или станция на одной линии) бот определит сам по отключениям; "
        "задать вручную: <code>/lines пбс ats</code> (или 1, 2, auto). Убрать вторую линию: <code>/queue пбс 3.1 0</code>.\n\n"
        "<b>Ёмкость АКБ</b>\n"
        "<code>/akb пбс 180</code> — ёмкость в кВт·ч. По ней бот считает клиенту, на сколько хватит заряда. "
        "<code>/akb пбс 0</code> — убрать вписанную. <code>/akb пбс</code> — показать, что вписано, что передаёт Deye и что получилось по факту отключений.\n\n"
        "<b>Назвать объект по-человечески</b>\n"
        "<code>/title пбс = Офис на Хуторской</code> — так объект будет называться в сообщениях клиенту.\n\n"
        "<b>Контакт техподдержки для клиентов</b>\n"
        "<code>/contact Имя Фамилия, +380 00 000 00 00, @username</code> — клиент увидит его в памятке бота и в сообщениях о проблемах.\n\n"
        "<b>Отключить клиента</b>\n"
        "<code>/reset пбс</code> — отключает всех клиентов станции и выдаёт новую ссылку. Старая перестаёт работать.\n\n"

        "<b>Эта памятка</b>\n"
        "<code>/help</code>\n\n"
        "В группу бот сам пишет только то, что требует действий: ошибки инвертора и АКБ, что стоит поменять в настройках или предложить клиенту, и кто из клиентов подключился. Про свет и графики он пишет клиентам.")


# ---------------------------------------------------------------- меню клиента
BTN_STATUS, BTN_SCHED, BTN_HELP, BTN_QUEUE = "🔌 Стан станції", "🗓 Графік", "❓ Допомога", "🔁 Змінити чергу"
BTN_NOTIFY = "🔔 Графіки: увімк./вимк."
KEYBOARD = {"keyboard": [[{"text": BTN_STATUS}, {"text": BTN_SCHED}], [{"text": BTN_QUEUE}, {"text": BTN_NOTIFY}], [{"text": BTN_HELP}]],
            "resize_keyboard": True, "is_persistent": True}
CLIENT_COMMANDS = [("status", "Стан станції: світло, заряд, навантаження"),
                   ("grafik", "Графік відключень ДТЕК на сьогодні і завтра"),
                   ("cherga", "Змінити чергу ДТЕК"),
                   ("grafiky", "Увімкнути або вимкнути графіки ДТЕК"),
                   ("help", "Що вміє бот і як змінити чергу"),
                   ("stop", "Вимкнути сповіщення")]
STAFF_COMMANDS = [("link", "Ссылка для клиента: /link пбс"),
                  ("stations", "Станции: очередь, клиенты, свет"),
                  ("queue", "Очередь ДТЭК: /queue пбс 3.1"),
                  ("lines", "Схема двух линий: /lines пбс ats"),
                  ("akb", "Ёмкость АКБ, кВт·ч: /akb пбс 180"),
                  ("title", "Название для клиента: /title пбс = Офис"),
                  ("contact", "Контакт техподдержки для клиентов"),
                  ("reset", "Отключить клиентов станции"),
                  ("links", "Все ссылки"),
                  ("help", "Памятка")]


def queue_grid(prefix, extra=None):
    """Кнопки очередей 1.1 … 6.2. prefix - что вернётся при нажатии."""
    rows = [[{"text": f"{a}.{b}", "callback_data": f"{prefix}{a}.{b}"} for a in (x, x + 1) for b in (1, 2)] for x in (1, 3, 5)]
    if extra:
        rows.append([extra])
    return {"inline_keyboard": rows}


def ask_queue(tg, st, chat, change=False, sid=None):
    mine = [k for k, p in st["stations"].items() if chat in (p.get("clients") or [])]
    if sid is None and len(mine) > 1:  # несколько объектов - сначала выбрать объект
        tg.send(chat, "Для якого об'єкта вказати чергу ДТЕК?", {"inline_keyboard": [[
            {"text": str(st["stations"][k].get("title") or st["stations"][k].get("name"))[:40], "callback_data": f"cs:{k}"}] for k in mine]})
        return
    sid = sid or (mine[0] if mine else None)
    if not sid:
        return
    p = st["stations"][sid]
    cur = queue_key(p.get("queue"))
    name = f"<b>{esc(p.get('title') or p.get('name'))}</b>. " if len(mine) > 1 else ""
    head = (f"{name}Зараз черга ДТЕК: <b>{cur.replace('GPV', '')}</b>.\nОберіть нову:" if cur
            else f"{name}Оберіть вашу чергу ДТЕК — так бот знатиме ваш графік відключень:")
    kb = queue_grid(f"cq:{sid}:", {"text": "Не знаю свою чергу", "callback_data": "cq:?"})
    if cur:  # друга лінія - тільки коли основна черга вже є
        q2 = queue_key(p.get("queue2"))
        kb["inline_keyboard"].append([{"text": f"Друга лінія: {q2.replace('GPV', '')} — змінити", "callback_data": f"c2:{sid}"},
                                      {"text": "Прибрати другу лінію", "callback_data": f"cx:{sid}"}] if q2 else
                                     [{"text": "➕ На об'єкті дві лінії (дві черги)", "callback_data": f"c2:{sid}"}])
    tg.send(chat, head, kb)


def sched_toggle(tg, st, chat, sid=None):
    """Клиент включает/выключает графики ДТЭК. Отдельно по каждому объекту."""
    mine = [k for k, p in st["stations"].items() if chat in (p.get("clients") or [])]
    if not mine:
        return
    if sid is None and len(mine) > 1:
        rows = [[{"text": f"{'🔔' if sched_on(st['stations'][k], chat) else '🔕'} {str(st['stations'][k].get('title') or st['stations'][k].get('name'))[:34]}",
                  "callback_data": f"gs:{k}"}] for k in mine]
        tg.send(chat, "Графіки ДТЕК по об'єктах (🔔 увімкнено, 🔕 вимкнено). Натисніть на об'єкт, щоб змінити:", {"inline_keyboard": rows})
        return
    sid = sid if sid in mine else mine[0]
    p = st["stations"][sid]
    off = p.setdefault("nosched", [])
    title = esc(p.get("title") or p.get("name"))
    if str(chat) in off:
        off.remove(str(chat))
        tg.send(chat, f"🔔 <b>{title}</b>: графіки ДТЕК увімкнено — графік на сьогодні й завтра, зміни і нагадування за 30 хвилин.", KEYBOARD)
    else:
        off.append(str(chat))
        tg.send(chat, f"🔕 <b>{title}</b>: графіки ДТЕК вимкнено. Повідомлення про світло, заряд, зв'язок і екстрені відключення приходять як і раніше.\n"
                      f"Подивитися графік вручну — кнопка «{BTN_SCHED}». Увімкнути знову — «{BTN_NOTIFY}».", KEYBOARD)
    if not off:
        p.pop("nosched", None)


def match_stations(st, text):
    t = str(text or "").strip().lower()
    if not t:
        return []
    exact = [sid for sid, p in st["stations"].items()
             if t == sid or t == str(p.get("name", "")).strip().lower() or t == str(p.get("title", "")).strip().lower()]
    if exact:  # полное название как в Deye - берём сразу, даже если оно часть других названий
        return exact
    return [sid for sid, p in st["stations"].items()
            if t in str(p.get("name", "")).lower() or t in str(p.get("title", "")).lower()]


def pick_station(tg, st, action, prompt, sids=None, all_button=None):
    """Список станций кнопками: нажал - команда выполнилась. action - буква команды."""
    sids = (sids if sids is not None else list(st["stations"]))[:60]
    btns = [{"text": str(st["stations"][k].get("title") or st["stations"][k].get("name"))[:28], "callback_data": f"{action}:{k}"} for k in sids]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if all_button:
        rows.append([all_button])
    more = "" if len(st["stations"]) <= 60 or len(sids) < 60 else "\nПоказаны первые 60. Чтобы сузить, допишите часть названия после команды."
    tg.send(tg.staff, prompt + more, {"inline_keyboard": rows})


PICK = {"/link": ("L", "Для какой станции нужна ссылка?"), "/stations": ("S", "Какую станцию показать?"),
        "/queue": ("Q", "У какой станции задать очередь ДТЭК?"), "/reset": ("R", "У какой станции отключить клиентов?")}


def callback(tg, st, chat, data, who, dtek):
    """Нажатие кнопки под сообщением."""
    kind, _, rest = (data or "").partition(":")
    if kind == "cs":  # клиент выбрал объект
        if chat in (st["stations"].get(rest, {}).get("clients") or []):
            ask_queue(tg, st, chat, sid=rest)
        return
    mine = st["stations"].get(rest.split(":")[0], {}).get("clients") or []
    if kind == "gs":  # графики вкл/выкл по объекту
        if chat in mine:
            sched_toggle(tg, st, chat, rest)
        return
    if kind == "qk":  # «моя черга правильна» - 14 дней не спрашиваем
        if chat in mine:
            st["stations"][rest]["qok_until"] = (now() + timedelta(days=14)).isoformat()
            tg.send(chat, "Добре, чергу залишено без змін.", KEYBOARD)
        return
    if kind in ("c2", "cr", "cx"):  # вторая линия
        c_sid, _, q = rest.partition(":")
        if chat not in mine:
            return
        p = st["stations"][c_sid]
        if kind == "c2":
            tg.send(chat, "Оберіть чергу ДТЕК другої лінії:", queue_grid(f"cr:{c_sid}:"))
        elif kind == "cx":
            p.pop("queue2", None)
            p.pop("lines_mode", None)
            tg.send(chat, "Другу лінію прибрано.", KEYBOARD)
            tg.staff_msg(f"👤 {who}: <b>{esc(p.get('name'))}</b> — вторая линия убрана, очередь {queue_key(p.get('queue')).replace('GPV', '')}.")
        elif re.fullmatch(r"[1-6]\.[12]", q) and queue_key(q) != queue_key(p.get("queue")):
            p["queue2"], p["lines_mode"] = queue_key(q), "auto"
            tg.send(chat, f"✅ Записано: дві лінії, черги {queue_key(p.get('queue')).replace('GPV', '')} і {q}. Графіки приходитимуть по обох.", KEYBOARD)
            tg.staff_msg(f"👤 {who}: <b>{esc(p.get('name'))}</b> — две линии, очереди {queue_key(p.get('queue')).replace('GPV', '')} и {q}. "
                         "Схему (АВР или станция на одной линии) бот определит сам по 2–3 отключениям.")
        return
    if kind == "cq":  # клиент выбрал очередь
        if rest == "?":
            tg.send(chat, HOWTO + "\n\nКоли дізнаєтесь — натисніть «" + BTN_QUEUE + "» і оберіть її.", KEYBOARD)
        else:
            c_sid, _, q = rest.rpartition(":")
            client_queue(tg, st, chat, q, who, c_sid or None)
        return
    if chat != str(tg.staff):
        return
    sid, _, arg = rest.partition(":")
    if kind == "S" and sid == "*":
        staff_command(tg, st, "/stations", "*")
    elif sid not in st["stations"]:
        tg.staff_msg("Этой станции уже нет в списке. Наберите команду заново.")
    elif kind == "L":
        staff_command(tg, st, "/link", sid)
    elif kind == "S":
        staff_command(tg, st, "/stations", sid)
    elif kind == "Q":
        p = st["stations"][sid]
        cur = (queue_key(p.get("queue")) or "не указана").replace("GPV", "")
        tg.send(tg.staff, f"<b>{esc(p.get('name'))}</b>: сейчас очередь {cur}. Выберите новую:", queue_grid(f"q:{sid}:"))
    elif kind == "q":
        staff_command(tg, st, "/queue", f"{sid} {arg}")
    elif kind == "R":
        p = st["stations"][sid]
        tg.send(tg.staff, f"Отключить всех клиентов станции <b>{esc(p.get('name'))}</b> ({len(p.get('clients') or [])}) и выдать новую ссылку? Старая ссылка перестанет работать.",
                {"inline_keyboard": [[{"text": "Да, отключить", "callback_data": f"r:{sid}"}, {"text": "Отмена", "callback_data": "x:"}]]})
    elif kind == "r":
        staff_command(tg, st, "/reset", sid)


def support(st):
    """Строка с контактом техподдержки для сообщений клиенту."""
    c = st.get("contact")
    return f"\nТехнічна підтримка Solaris: {esc(c)}" if c else ""


def client_help(st):
    c = st.get("contact")
    return ("ℹ️ <b>Бот Solaris стежить за вашою станцією і сам пише, коли щось змінюється:</b>\n"
            "• вимкнули або увімкнули світло;\n"
            "• графік відключень ДТЕК і нагадування за 30 хвилин;\n"
            "• екстрені відключення;\n"
            "• станція зникла зі зв'язку — і чому;\n"
            "• звіт за місяць.\n\n"
            "<b>Кнопки внизу екрана:</b>\n"
            f"{BTN_STATUS} — чи є світло, заряд АКБ, навантаження.\n"
            f"{BTN_SCHED} — коли сьогодні й завтра не буде світла.\n"
            f"{BTN_QUEUE} — обрати іншу чергу ДТЕК зі списку або додати другу лінію.\n"
            f"{BTN_NOTIFY} — вимкнути або знову увімкнути графіки й нагадування, окремо по кожному об'єкту.\n"
            f"{BTN_HELP} — ця підказка.\n\n"
            "<b>Вимкнути сповіщення</b> — /stop"
            + (f"\n\n<b>Технічна підтримка Solaris:</b>\n{esc(c)}" if c else ""))


def apply_latest(p, lt, t):
    """Сохранить свежие данные станции. Время - по Deye, если оно есть."""
    at = deye_time(lt) or t
    old = (p.get("last") or {}).get("at")
    if old and at.isoformat() < old and deye_time(lt):
        return  # у нас уже есть данные новее
    p["last"] = {"soc": num(lt.get("batterySOC")), "load": num(lt.get("consumptionPower")) or 0,
                 "pv": num(lt.get("generationPower")), "dkw": discharge_kw(lt), "at": at.isoformat()}


def client_status(st, chat, api=None):
    out = []
    for sid, p in st["stations"].items():
        if chat not in (p.get("clients") or []):
            continue
        if api and not p.get("offline"):
            # Кнопка показывает свежие данные из Deye, а не то, что сохранила последняя проверка.
            try:
                if not api.token:
                    api.login()
                apply_latest(p, api.latest(int(sid)), now())
            except Exception:
                pass
        title = esc(p.get("title") or p.get("name"))
        last = p.get("last") or {}
        if p.get("offline"):
            out.append(f"🔴 <b>{title}</b>: станція не на зв'язку, свіжих даних немає."
                       + (f"\nОстанні дані: АКБ {last['soc']:.0f}%, о {last['at'][11:16]}." if last.get("soc") is not None else ""))
            continue
        if p.get("no_grid"):
            since = datetime.fromisoformat(p["off_since"])
            line = f"🟠 <b>{title}</b>: світла немає з {since_hm(since)} ({dur((now() - since).total_seconds() / 60)}). Об'єкт працює від станції."
        else:
            line = f"🟢 <b>{title}</b>: світло є."
        if last.get("soc") is not None:
            line += f"\nАКБ {last['soc']:.0f}%"
            if last.get("load") is not None:
                line += f" · навантаження {last['load'] / 1000:.1f} кВт"
            if p.get("no_grid"):
                left, how = runtime(st, sid, p, last["soc"], last.get("dkw"))
                if how == "pv":
                    line += "\nЗараз АКБ не розряджається: навантаження покривають сонячні панелі."
                elif left is not None and left > 0:
                    word = "За такого споживання" if how == "energy" else "За звичного споживання"
                    line += f"\n{word} заряду вистачить ще приблизно на {dur(left * 60)} (до залишку 15%)."
                    until = datetime.fromisoformat(p["off_until"]) if p.get("off_until") else None
                    if until and until > now():
                        ok = now() + timedelta(hours=left) >= until + timedelta(minutes=30)
                        line += f"\nЗа графіком світло мають дати о {hm(until)} — " + ("заряду має вистачити." if ok else "⚠️ заряду може не вистачити, вимкніть потужні прилади.")
            line += f"\nДані станом на {last.get('at', '')[11:16]}."
        out.append(line)
    return "\n\n".join(out)


def client_schedule(st, chat, dtek):
    out = []
    t = now()
    for p in st["stations"].values():
        if chat not in (p.get("clients") or []):
            continue
        title = esc(p.get("title") or p.get("name"))
        qs = queues(p)
        if not qs:
            out.append(f"🗓 <b>{title}</b>: черга ДТЕК ще не вказана. Надішліть дві цифри, наприклад: 3.1")
            continue
        if not dtek:
            out.append(f"🗓 <b>{title}</b>: графік ДТЕК зараз недоступний, спробуйте за кілька хвилин.")
            continue
        lines = [f"🗓 <b>{title}</b>" + ("" if sched_on(p, chat) else " (сповіщення про графіки вимкнено)")]
        for n, q in enumerate(qs, 1):
            lines.append((f"<b>Лінія {n}</b>, " if len(qs) > 1 else "") + f"черга {q.replace('GPV', '')}")
            for day, label in ((t, "Сьогодні"), (t + timedelta(days=1), "Завтра")):
                sl = (dtek["days"].get(day.strftime("%Y-%m-%d")) or {}).get(q)
                lines.append(f"<b>{label} ({day.strftime('%d.%m')}):</b> " + (fmt_intervals(sl) if sl is not None else "графік ще не опубліковано"))
        if dtek.get("updated"):
            lines.append(f"Оновлено ДТЕК: {esc(dtek['updated'])}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def bot_setup(tg, st):
    """Меню команд, описание и аватар бота. Запускается вручную режимом setup."""
    res = {}
    res["команды клиентов"] = tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in CLIENT_COMMANDS],
                                      scope={"type": "all_private_chats"}).get("ok")
    res["команды группы"] = tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in STAFF_COMMANDS],
                                    scope={"type": "chat", "chat_id": int(tg.staff)}).get("ok")
    res["описание"] = tg.call("setMyDescription", description="Solaris — сонячні електростанції.\n\nБот стежить за вашою станцією і пише, коли вимикають і вмикають світло, надсилає графік відключень ДТЕК і звіт за місяць.\n\nЩоб підключитися, відкрийте посилання від вашого менеджера.").get("ok")
    res["короткое описание"] = tg.call("setMyShortDescription", short_description="Сповіщення про світло і стан вашої сонячної станції Solaris").get("ok")
    res["кнопка меню"] = tg.call("setChatMenuButton", menu_button={"type": "commands"}).get("ok")
    try:
        with open(os.path.join(HERE, "assets", "avatar.jpg"), "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{tg.token}/setMyProfilePhoto",
                              data={"photo": json.dumps({"type": "static", "photo": "attach://ava"})}, files={"ava": f}, timeout=30)
        res["аватар"] = r.json().get("ok")
    except Exception:
        res["аватар"] = False
    tg.staff_msg("🎨 Оформление бота:\n" + "\n".join(f"{'✅' if v else '❌'} {k}" for k, v in res.items())
                 + ("" if res.get("аватар") else "\n\nАватар через программу поставить не удалось — поставьте вручную: @BotFather → /setuserpic → выбрать бота → отправить картинку."))
    print("setup:", {k: bool(v) for k, v in res.items()})


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
        with open(STATE + ".inbox", "a", encoding="utf-8") as f:  # список обработанных - для шага сохранения
            f.write(os.path.basename(path) + "\n")
    return out


def commands(tg, st, dtek=None, api=None):
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
                          allowed_updates=json.dumps(["message", "callback_query"])) or []
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
        cq = u.get("callback_query")
        if cq:
            c_chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
            c_who = esc(" ".join(x for x in ((cq.get("from") or {}).get("first_name"), (cq.get("from") or {}).get("last_name")) if x) or "клиент")
            if c_chat:
                mid = (cq.get("message") or {}).get("message_id")
                if mid and str(cq.get("data", "")).split(":")[0] in ("cq", "cs", "q", "r", "x", "L", "S", "Q", "R", "qk", "gs", "c2", "cr", "cx") and cq.get("data") != "cq:?":
                    tg.call("editMessageReplyMarkup", chat_id=c_chat, message_id=mid, reply_markup={"inline_keyboard": []})
                callback(tg, st, c_chat, cq.get("data"), c_who, dtek)
            continue
        m = u.get("message") or {}
        chat = str((m.get("chat") or {}).get("id", ""))
        text = (m.get("text") or "").strip()
        private = (m.get("chat") or {}).get("type", "private") == "private"
        if not chat or not text or (not text.startswith("/") and (chat == str(tg.staff) or not private)):
            continue
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        if chat != str(tg.staff):  # кнопки внизу экрана у клиента - это те же команды
            cmd = {BTN_STATUS: "/status", BTN_SCHED: "/grafik", BTN_HELP: "/help", BTN_QUEUE: "/cherga", BTN_NOTIFY: "/grafiky"}.get(text, cmd)
        is_client = any(chat in (p.get("clients") or []) for p in st["stations"].values())
        who = esc(" ".join(x for x in ((m.get("from") or {}).get("first_name"), (m.get("from") or {}).get("last_name")) if x) or "клиент")
        if chat == str(tg.staff):
            staff_command(tg, st, cmd, arg)
        elif cmd == "/start":
            sid = next((k for k, p in st["stations"].items() if arg and p.get("code") == arg), None)
            if not sid:
                tg.send(chat, "Вітаємо! Це бот сповіщень Solaris. Щоб підключитися, відкрийте посилання, яке вам надіслав менеджер.")
                continue
            p = st["stations"][sid]
            p.setdefault("names", {})[chat] = who
            if chat in (p.get("clients") or []):
                # уже подключён (ссылку открыли или Start нажали ещё раз) - в группу не пишем, заново не приветствуем
                tg.send(chat, f"Ви вже підключені до <b>{esc(p.get('title') or p.get('name'))}</b>. Кнопки внизу екрана працюють.", KEYBOARD)
                if not queue_key(p.get("queue")):
                    ask_queue(tg, st, chat, sid=sid)
                continue
            p.setdefault("clients", []).append(chat)
            title = esc(p.get("title") or p.get("name"))
            ask = ""
            tg.photo(chat, os.path.join(HERE, "assets", "logo.jpg"),
                     f"✅ Підключено: <b>{title}</b>.\nСюди надходитимуть повідомлення, коли на об'єкті вимикають і вмикають світло, графіки ДТЕК і звіт за місяць.{ask}", KEYBOARD)
            if tg.send(chat, client_help(st), KEYBOARD) and tg.last_id:  # памятку закрепляем вверху чата
                tg.call("pinChatMessage", chat_id=chat, message_id=tg.last_id, disable_notification=True)
            if not queue_key(p.get("queue")):
                ask_queue(tg, st, chat)
            if queue_key(p.get("queue")):
                tg.staff_msg(f"👤 {who} подключился к <b>{title}</b>. Очередь ДТЭК {p['queue'].replace('GPV', '')}.")
            else:  # ждём, пока пришлёт очередь, и напишем одним сообщением
                p.setdefault("joins", {})[chat] = {"who": who, "at": now().isoformat()}
        elif cmd in ("/status", "/grafik", "/help") and is_client:
            tg.send(chat, {"/status": lambda: client_status(st, chat, api), "/grafik": lambda: client_schedule(st, chat, dtek),
                           "/help": lambda: client_help(st)}[cmd](), KEYBOARD)
        elif cmd == "/cherga" and is_client:
            ask_queue(tg, st, chat, change=True)
        elif cmd == "/grafiky" and is_client:
            sched_toggle(tg, st, chat)
        elif cmd == "/queue" or not text.startswith("/") or not is_client:
            client_queue(tg, st, chat, arg if cmd == "/queue" else text, who)
        elif cmd == "/stop":
            n = 0
            for p in st["stations"].values():
                if chat in (p.get("clients") or []):
                    p["clients"].remove(chat)
                    n += 1
            tg.send(chat, "Сповіщення вимкнено. Щоб увімкнути знову, відкрийте посилання від менеджера." if n else "Ви не були підключені.", {"remove_keyboard": True})
            for p in st["stations"].values():
                (p.get("joins") or {}).pop(chat, None)
            if n:
                tg.staff_msg(f"👤 {who} отключил уведомления.")


HOWTO = ("Дізнатися чергу просто, це 1 хвилина:\n"
         "1. Відкрийте сторінку ДТЕК Одеські електромережі: https://www.dtek-oem.com.ua/ua/shutdowns\n"
         "2. Закрийте вікно з оголошенням і вгорі сторінки заповніть три поля: населений пункт, вулиця, номер будинку.\n"
         "3. Під адресою з'явиться графік і номер вашої черги, наприклад «3.1».\n"
         "4. Оберіть цю чергу кнопкою в боті.\n\n"
         "Чергу також показує чат-бот ДТЕК у Viber: https://chats.viber.com/dtekodeskielektromerezhi\n"
         "Не виходить — напишіть у технічну підтримку адресу об'єкта, вам підкажуть.")


def client_queue(tg, st, chat, text, who, sid=None):
    """Клиент прислал очередь («3.1», «очередь 3,1», «3 1») или что-то другое."""
    mine = [k for k, p in st["stations"].items() if chat in (p.get("clients") or [])]
    if not mine:
        tg.send(chat, "Вітаємо! Це бот сповіщень Solaris. Щоб підключитися, відкрийте посилання, яке вам надіслав менеджер.")
        return
    m = re.search(r"(?<!\d)([1-6])\s*[.,/ ]\s*([12])(?!\d)", text or "")
    if not m:
        if queue_key(st["stations"][mine[0]].get("queue")) and len(mine) == 1:
            q = st["stations"][mine[0]]["queue"].replace("GPV", "")
            tg.send(chat, f"Ваша черга ДТЕК зараз: {q}. Щоб змінити її, натисніть «{BTN_QUEUE}».\nЗ інших питань напишіть, будь ласка, у технічну підтримку.", KEYBOARD)
        else:
            ask_queue(tg, st, chat)
        return
    q = f"{m.group(1)}.{m.group(2)}"
    if sid not in mine:
        sid = None
    if len(mine) > 1 and not sid:
        ask_queue(tg, st, chat)  # спросим объект, потом очередь
        return
    p = st["stations"][sid or mine[0]]
    joined = (p.get("joins") or {}).pop(chat, None)
    if queue_key(p.get("queue")) == "GPV" + q and not joined:
        # та же очередь, что уже стоит (например, кнопку нажали несколько раз) - в группу не пишем
        tg.send(chat, f"Черга ДТЕК {q} вже встановлена.", KEYBOARD)
        return
    p["queue"] = "GPV" + q
    tg.send(chat, f"✅ Чергу ДТЕК {q} записано. Графік відключень надійде за кілька хвилин.")
    if joined:  # в группу пишем один раз - при подключении; смену очереди потом клиент делает молча
        tg.staff_msg(f"👤 {who} подключился к <b>{esc(p.get('title') or p.get('name'))}</b>. Очередь ДТЭК {q}.")


def staff_command(tg, st, cmd, arg):
    S, bot = st["stations"], st.get("tg", {}).get("bot", "")
    if not S and cmd != "/help":
        tg.staff_msg("Станции ещё не загружены, повторите команду через 10 минут.")
        return
    if cmd == "/stations" and not arg:
        arg = "*"
    if cmd in PICK and arg != "*":
        # Станций много, поэтому списком их не показываем: нужно название как в Deye Cloud.
        name = re.sub(r"(\s+(\d\s*[.,]\s*\d|0|-))+\s*$", "", arg) if cmd == "/queue" else arg
        letter, prompt = PICK[cmd]
        example = {"/link": "/link ПБС", "/stations": "/stations ПБС", "/queue": "/queue ПБС 3.1", "/reset": "/reset ПБС"}[cmd]
        if not name:
            tg.staff_msg(f"Напишите после команды название станции, как в Deye Cloud. Пример: <code>{example}</code>\n"
                         "Не помните точно — напишите часть названия, покажу подходящие.")
            return
        hits = match_stations(st, name)
        if not hits:
            tg.staff_msg(f"Станция «{esc(name)}» не найдена. Проверьте название в Deye Cloud или напишите его часть.")
            return
        if len(hits) > 12:
            tg.staff_msg(f"К «{esc(name)}» подходит {len(hits)} станций. Напишите название точнее.")
            return
        if len(hits) > 1:
            pick_station(tg, st, letter, f"К «{esc(name)}» подходит несколько станций. {prompt}", hits)
            return
        if cmd == "/queue" and name == arg:  # станция есть, очередь не написали
            callback(tg, st, str(tg.staff), f"Q:{hits[0]}", "", None)
            return
        if cmd == "/reset" and arg != hits[0]:  # набрано текстом - переспросим
            callback(tg, st, str(tg.staff), f"R:{hits[0]}", "", None)
            return
    if cmd == "/stations" and arg and arg != "*":
        sid, err = find_station(st, arg)
        if err:
            tg.staff_msg(err)
            return
        p = S[sid]
        cap, src = capacity(st, sid, p)
        tg.staff_msg(f"<b>{esc(p.get('name'))}</b>" + (f" → «{esc(p['title'])}»" if p.get("title") else "") +
                     f"\nочередь ДТЭК: {' и '.join(q.replace('GPV', '') for q in queues(p)) or 'не указана'} · клиентов: {len(p.get('clients') or [])}"
                     + (f"\nдве линии: {MODE_RU.get(p.get('lines_mode'), 'схема ещё не определена')}" if p.get("queue2") else "")
                     + (f"\nграфики отключили клиенты: {len(p['nosched'])}" if p.get("nosched") else "") +
                     f"\nсеть: {'НЕТ с ' + since_hm(datetime.fromisoformat(p['off_since'])) if p.get('no_grid') else 'есть'} · связь: {'нет' if p.get('offline') else 'есть'}"
                     f"\nёмкость АКБ: {f'{cap:.0f} кВт·ч ({src})' if cap else 'неизвестна — задайте: /akb ' + esc(p.get('name')) + ' 180'}")
    elif cmd == "/akb":
        m = re.match(r"(.+?)\s+(\d+(?:[.,]\d+)?)\s*(квт.*|kwh)?\s*$", arg, re.I)
        sid, err = find_station(st, m.group(1) if m else arg)
        if err:
            tg.staff_msg(err if arg else "Пример: /akb ПБС 180 — ёмкость АКБ в кВт·ч. Без числа — показать, что сейчас.")
            return
        p = S[sid]
        if m:
            val = float(m.group(2).replace(",", "."))
            if val == 0:  # /akb name 0 - убрать вписанную вручную
                p.pop("cap_kwh", None)
                tg.staff_msg(f"✅ <b>{esc(p.get('name'))}</b>: вписанная ёмкость убрана, бот снова берёт её из Deye и по факту отключений.")
                return
            if not 1 <= val <= 5000:
                tg.staff_msg("Ёмкость пишется в кВт·ч, например /akb ПБС 180.")
                return
            p["cap_kwh"] = val
        fact = learned_cap(st, sid)
        tg.staff_msg((f"✅ <b>{esc(p.get('name'))}</b>: ёмкость АКБ {p['cap_kwh']:g} кВт·ч. По ней считается, на сколько хватит заряда.\n" if m else f"<b>{esc(p.get('name'))}</b>, ёмкость АКБ:\n")
                     + f"вписана: {str(p['cap_kwh']) + ' кВт·ч' if p.get('cap_kwh') else 'нет'}\n"
                     f"из Deye: {str(p['cap_deye']) + ' кВт·ч' if p.get('cap_deye') else 'Deye не передаёт'}\n"
                     f"по факту отключений: {f'~{fact:.0f} кВт·ч' if fact else 'пока мало данных (нужно 2 отключения с падением заряда от 10%)'}")
    elif cmd == "/stations":
        def names(ps, n=15):
            lst = [esc(p.get("name")) for p in ps]
            return ", ".join(lst[:n]) + (f" и ещё {len(lst) - n}" if len(lst) > n else "") if lst else "нет"
        P = list(S.values())
        dark = [p for p in P if p.get("no_grid") and not p.get("offline")]
        off = [p for p in P if p.get("offline")]
        low = [p for p in P if (p.get("last") or {}).get("soc") is not None and p["last"]["soc"] < 40 and not p.get("offline")]
        tg.staff_msg(f"📊 <b>Станций: {len(P)}</b>\n\n"
                     f"🟠 Без света сейчас ({len(dark)}): {names(dark)}\n"
                     f"🔴 Не на связи ({len(off)}): {names(off)}\n"
                     f"🔋 АКБ ниже 40% ({len(low)}): {names(low)}\n\n"
                     f"Без очереди ДТЭК: {len([p for p in P if not queue_key(p.get('queue'))])}\n"
                     f"Без подключённых клиентов: {len([p for p in P if not p.get('clients')])}\n\n"
                     "Подробно по одной: <code>/stations название</code>")
    elif cmd in ("/link", "/links") and arg:
        sid, err = find_station(st, arg)
        if err:
            tg.staff_msg(err)
            return
        p = S[sid]
        tg.staff_msg(f"Ссылка для клиента, <b>{esc(p.get('title') or p.get('name'))}</b>:\nhttps://t.me/{bot}?start={p.get('code')}")
    elif cmd == "/links" and len(S) > 25:
        tg.staff_msg("Станций слишком много для одного сообщения. Берите ссылку по одной: <code>/link название</code>")
    elif cmd == "/links":
        tg.staff_msg("Ссылки для клиентов (кто откроет и нажмёт Start — будет получать уведомления по станции):\n\n" + "\n\n".join(
            f"<b>{esc(p.get('title') or p.get('name'))}</b>\nhttps://t.me/{bot}?start={p.get('code')}" for p in S.values() if p.get("code")))
    elif cmd == "/queue":
        m = re.match(r"(.+?)\s+(\d\s*[.,]\s*\d)(?:\s+(\d\s*[.,]\s*\d|0|-))?\s*$", arg)
        if not m:
            tg.staff_msg("Пример: /queue ПБС 3.1\nДве линии: /queue ПБС 3.1 5.2 · убрать вторую: /queue ПБС 3.1 0")
            return
        sid, err = find_station(st, m.group(1))
        if err:
            tg.staff_msg(err)
            return
        p = S[sid]
        p["queue"] = queue_key(m.group(2))
        if m.group(3) in ("0", "-"):
            p.pop("queue2", None)
        elif m.group(3) and queue_key(m.group(3)) != p["queue"]:
            if queue_key(m.group(3)) != p.get("queue2"):
                p["queue2"], p["lines_mode"] = queue_key(m.group(3)), "auto"
        txt = " и ".join(q.replace("GPV", "") for q in queues(p))
        extra = ("\nДве линии. Схему (АВР или станция на одной линии) бот определит сам по 2–3 отключениям и напишет сюда. "
                 f"Задать вручную: <code>/lines {esc(p.get('name'))} ats</code> (или 1, 2)." if p.get("queue2") and p.get("lines_mode", "auto") == "auto" else "")
        tg.staff_msg(f"✅ <b>{esc(p.get('name'))}</b>: очередь ДТЭК {txt}. График придёт при следующей проверке.{extra}")
    elif cmd == "/lines":
        m = re.match(r"(.+?)\s+(ats|авр|1|2|auto|авто)\s*$", arg, re.I)
        sid, err = find_station(st, m.group(1) if m else arg)
        if err or not m:
            tg.staff_msg(err or "Пример: /lines ПБС ats — схема двух линий: ats (АВР), 1 или 2 (станция на этой линии), auto (бот определит сам).")
            return
        mode = {"ats": "ats", "авр": "ats", "1": "q1", "2": "q2", "auto": "auto", "авто": "auto"}[m.group(2).lower()]
        S[sid]["lines_mode"] = mode
        tg.staff_msg(f"✅ <b>{esc(S[sid].get('name'))}</b>: " + (MODE_RU[mode] if mode != "auto" else "схему двух линий бот определит сам") + ".")
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
        tg.clients(S[sid].get("clients"), "Сповіщення по об'єкту вимкнено менеджером.", urgent=True)
        S[sid]["clients"], S[sid]["code"] = [], secrets.token_urlsafe(9)
        tg.staff_msg(f"✅ <b>{esc(S[sid].get('name'))}</b>: клиенты отключены. Новая ссылка:\nhttps://t.me/{bot}?start={S[sid]['code']}")
    elif cmd == "/contact":
        if not arg:
            tg.staff_msg("Сейчас: " + (esc(st.get("contact")) or "не задан") + "\nПример: /contact Имя Фамилия, +380 00 000 00 00, @username")
            return
        st["contact"] = arg[:120]
        tg.staff_msg(f"✅ Контакт техподдержки для клиентов: {esc(st['contact'])}\nОн показывается клиенту в памятке (кнопка «Допомога»).")
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
# ---------------------------------------------------------------- неисправности (только в группу)
CAUSES = [
    (r"(BAT|DC).*Volt.?Low|Volt.?Low.*(BAT|DC)",
     "низкое напряжение АКБ / шины DC",
     "АКБ разрядился до отключения (долго не было света); BMS отключил батарею; занижено напряжение или % отключения в настройках; плохой контакт или тонкий кабель АКБ.",
     "Проверить, до какого % разряжалась батарея и стоит ли заряд от сети. Если повторяется при отключениях — ёмкости АКБ не хватает."),
    (r"DC.*Over.?Curr",
     "перегрузка по току на стороне DC (АКБ или панели)",
     "резкий пуск мощной нагрузки от батареи; ток заряда/разряда в настройках выше, чем разрешает BMS; плохой контакт на клеммах АКБ; неисправность батареи.",
     "Сверить Max A Charge / Discharge с паспортом АКБ, проверить клеммы. Если совпадает с пуском оборудования — разделить нагрузки."),
    (r"AC.*Over.?Curr|Over.?Load",
     "перегрузка по выходу AC",
     "нагрузка больше мощности инвертора; пусковые токи моторов, насосов, компрессоров; короткое замыкание в линии.",
     "Посмотреть нагрузку в момент ошибки. Убрать мощные приборы с резервной линии или ставить инвертор мощнее."),
    (r"Insul|ISO|Leak|GFDI",
     "утечка / низкая изоляция",
     "влага в коннекторах или кабеле панелей; повреждён кабель PV; проблема с заземлением.",
     "Проверить изоляцию стрингов и коннекторы MC4, заземление. Чаще проявляется в сырую погоду."),
    (r"BMS|Comm|CAN|Parallel",
     "нет связи (BMS / параллель / CAN)",
     "отошёл или повреждён кабель связи с АКБ; неверный протокол батареи; сбой после перезапуска.",
     "Проверить кабель BMS и выбранный протокол Lithium Mode. Без связи с BMS инвертор не видит реальный заряд."),
    (r"Temp|Heat",
     "перегрев",
     "плохая вентиляция, пыль в радиаторе, не работает вентилятор, жаркое помещение.",
     "Проверить вентиляторы и свободное место вокруг инвертора."),
    (r"Grid.*(Volt|Freq)|AC.*(Volt|Freq)",
     "параметры сети вне нормы",
     "просадки или скачки напряжения в сети, плохой ввод; это проблема сети, а не инвертора.",
     "Если часто — проверить ввод и сечение кабеля, расширить допустимые пределы сети в настройках в рамках паспорта."),
    (r"ARC", "дуговой пробой (ARC)", "плохой контакт в цепи панелей, повреждённый коннектор.", "Срочно проверить коннекторы и кабели PV."),
    (r"Bus", "ошибка шины DC", "резкий сброс/наброс нагрузки, проблема АКБ, реже — неисправность инвертора.", "Если повторяется — обращаться в сервис."),
]


def explain(name):
    for rx, what, why, do in CAUSES:
        if re.search(rx, name or "", re.I):
            return what, why, do
    return "ошибка инвертора", "по этому коду готового объяснения нет.", "Посмотреть код в инструкции к инвертору или спросить поддержку Deye."


def device_alerts(tg, p, title, items, t):
    """Ошибки и аварии инвертора из Deye Cloud -> в группу, с возможными причинами."""
    seen = set(p.get("al_seen") or [])
    first = "al_seen" not in p
    lastn = p.setdefault("al_last", {})
    fresh = {}
    for a in items:
        aid = str(a.get("alertId"))
        if aid in seen:
            continue
        seen.add(aid)
        started = num(a.get("alertStartTime")) or 0
        if first and time.time() - started > 7200:
            continue  # при первом запуске старое не пересылаем
        fresh.setdefault(str(a.get("alertName") or "?").replace("\xa0", " "), []).append(a)
    p["al_seen"] = sorted(seen)[-150:]
    for name, lst in fresh.items():
        prev = lastn.get(name)
        if prev and (t - datetime.fromisoformat(prev)).total_seconds() < 6 * 3600:
            continue  # эту же ошибку уже присылали недавно
        lastn[name] = t.isoformat()
        a = max(lst, key=lambda x: num(x.get("alertStartTime")) or 0)
        when = datetime.fromtimestamp(num(a.get("alertStartTime")) or time.time(), KYIV)
        end = num(a.get("alertEndTime"))
        state = f"длилась {dur_ru((end - num(a.get('alertStartTime'))) / 60)}, сейчас снята" if end and end > (num(a.get("alertStartTime")) or 0) else "<b>активна сейчас</b>"
        what, why, do = explain(name)
        many = f"\nЗа последние сутки таких ошибок: {len(lst)}." if len(lst) > 1 else ""
        tg.staff_msg(f"🛠 <b>{title}</b>: ошибка инвертора <code>{esc(name)}</code>\n"
                     f"{when.strftime('%d.%m %H:%M')}, {state}.{many}\n\n"
                     f"<b>Что это:</b> {what}.\n<b>Возможные причины:</b> {why}\n<b>Что сделать:</b> {do}"
                     "\n\n<b>Откуда это:</b> ошибку записал сам инвертор, программа взяла её из журнала Deye Cloud. Причины — типовые для такого вида ошибки, точную покажет осмотр на месте.")


def problems(tg, st, p, title, dev, soc, load, low_soc, t, no_grid):
    """Неисправности, которые видно по текущим данным: АКБ, прошивка."""
    # 1. нет данных от АКБ. Около полуночи Deye Cloud на несколько минут перестаёт
    # отдавать заряд (обнуляет суточные данные), поэтому тревога — только если
    # данных нет 30+ минут подряд.
    if soc is None:
        p.setdefault("nosoc_since", t.isoformat())
        mins = (t - datetime.fromisoformat(p["nosoc_since"])).total_seconds() / 60
        if mins >= 30 and not p.get("nosoc_sent"):
            p["nosoc_sent"] = True
            tg.staff_msg(f"🛠 <b>{title}</b>: станция на связи, но не отдаёт заряд АКБ с {since_hm(datetime.fromisoformat(p['nosoc_since']))} ({dur_ru(mins)}).\n<b>Возможные причины:</b> нет связи инвертора с BMS (кабель, протокол), батарея отключена автоматом или ушла в защиту."
                         "\n\n<b>Как определено:</b> Deye Cloud присылает данные станции, но поле заряда АКБ пустое 30+ минут подряд. Короткие пропуски (например, около полуночи) не считаются.")
    else:
        if p.get("nosoc_sent"):
            tg.staff_msg(f"✅ <b>{title}</b>: данные АКБ снова приходят, заряд {soc:.0f}%.")
        for k in ("nosoc", "nosoc_since", "nosoc_sent"):
            p.pop(k, None)
    # 2. АКБ не заряжается при наличии сети
    if soc is not None and soc < low_soc and no_grid is False and not p.get("no_grid"):
        p.setdefault("lowgrid_since", t.isoformat())
        hrs = (t - datetime.fromisoformat(p["lowgrid_since"])).total_seconds() / 3600
        if hrs >= 3 and not p.get("lowgrid_sent"):
            p["lowgrid_sent"] = True
            tg.staff_msg(f"⚙️ <b>{title}</b>: сеть есть уже {hrs:.0f} ч, а АКБ всё ещё {soc:.0f}%.\n"
                         "<b>Что проверить в настройках:</b> включён ли заряд от сети (Grid Charge) и до какого % он идёт в Time of Use; не занижен ли ток заряда.\n"
                         "Зимой в Одессе свет могут отключить в любой момент — батарея должна успевать зарядиться между отключениями."
                         f"\n\n<b>Как посчитано:</b> напряжение сети на инверторе в норме с {p['lowgrid_since'][11:16]}, а заряд АКБ всё это время ниже порога {low_soc:.0f}%. Порог для сообщения: 3 часа.")
    else:
        p.pop("lowgrid_since", None)
        p.pop("lowgrid_sent", None)
    # 3. температура АКБ
    bt = num(dev.get("Temperature- Battery"))
    if bt is not None and (bt < 2 or bt > 48) and p.get("bt_day") != t.strftime("%Y-%m-%d"):
        p["bt_day"] = t.strftime("%Y-%m-%d")
        tg.staff_msg(f"🛠 <b>{title}</b>: температура АКБ {bt:.0f}°C.\n" + (
            "Литиевые батареи нельзя заряжать на морозе: BMS ограничит или запретит заряд, и к отключению света батарея может оказаться пустой. Нужен обогрев или перенос в тёплое помещение."
            if bt < 2 else "Перегрев сокращает срок службы и может вызвать отключение батареи. Проверить вентиляцию и нагрузку."))
    # 4. прошивка изменилась
    fw = {k: str(dev.get(k)) for k in ("MAIN", "HMI") if dev.get(k)}
    if fw:
        old = p.get("fw")
        if old and old != fw:
            tg.staff_msg(f"ℹ️ <b>{title}</b>: прошивка инвертора изменилась.\nБыло: MAIN {old.get('MAIN', '—')}, HMI {old.get('HMI', '—')}\nСтало: MAIN {fw.get('MAIN', '—')}, HMI {fw.get('HMI', '—')}")
        p["fw"] = fw
        p["rated"] = num(dev.get("RatedPower"))


def firmware_mismatch(tg, st):
    """У одинаковых инверторов разные прошивки - вероятно, один из них пора обновить."""
    groups = {}
    for p in st["stations"].values():
        if p.get("fw", {}).get("MAIN") and p.get("rated"):
            # семейство = мощность + первая часть версии (3103-...)
            groups.setdefault((p["rated"], p["fw"]["MAIN"].split("-")[0]), []).append(p)
    lines = []
    for (rated, _), ps in groups.items():
        if len({x["fw"]["MAIN"] for x in ps}) > 1:
            lines.append(f"Инверторы {rated / 1000:.0f} кВт:\n" + "\n".join(
                f"• {esc(x.get('title') or x.get('name'))}: MAIN {x['fw']['MAIN']}, HMI {x['fw'].get('HMI', '—')}" for x in ps))
    sig = hashlib.sha1("|".join(lines).encode()).hexdigest() if lines else ""
    if sig != st.get("fw_sig", ""):
        st["fw_sig"] = sig
        if lines:
            tg.staff_msg("🧩 <b>Прошивки не совпадают</b> у одинаковых инверторов:\n\n" + "\n\n".join(lines) +
                         "\n\n<b>Как определено:</b> сравниваются версии MAIN у инверторов одной мощности и одной серии (первая часть номера совпадает). "
                         "\nСкорее всего, на одном из них стоит более старая версия и его пора обновить. "
                         "Какая версия актуальная, программа не знает — это нужно уточнить у поддержки Deye, они же обновляют удалённо.")


# ---------------------------------------------------------------- рекомендации (только в группу)
WINTER = ("Зимой в Одессе отключения по графику идут по несколько часов подряд и могут занимать до половины суток, "
          "а после обстрелов вводят экстренные — без графика и на неопределённое время.")


def advice(tg, st, sid, p, title, dev, load, t):
    """Раз в сутки по станции: что стоит поменять в настройках или предложить клиенту. Не чаще раза в 14 дней на тему."""
    today = t.strftime("%Y-%m-%d")
    rated = p.get("rated")
    # накопление наблюдений
    if rated and load and load > 0.9 * rated:
        p["ovl"] = sorted(set((p.get("ovl") or []) + [today]))[-10:]
        p["ovl_max"] = max(p.get("ovl_max", 0), load)
    volts = [num(v) for k, v in (dev or {}).items() if k and VOLT_KEY.search(k) and "L1L2" not in k]
    volts = [v for v in volts if v and v > 50]
    if volts and min(volts) < 195:
        p["lowv"] = sorted(set((p.get("lowv") or []) + [today]))[-10:]
        p["lowv_min"] = min(p.get("lowv_min", 999), min(volts))
    if p.get("adv_day") == today or t.hour < 10:
        return
    p["adv_day"] = today
    sent = p.setdefault("adv", {})

    def due(key):
        last = sent.get(key)
        if last and (t - datetime.fromisoformat(last)).days < 14:
            return False
        sent[key] = t.isoformat()
        return True

    cut = (t - timedelta(days=30)).isoformat()
    outs = [r for r in st["log"] if r.get("sid") == sid and r.get("start", "") >= cut]
    # 1. хватает ли АКБ
    rates = sorted((r["soc_start"] - r["soc_end"]) / (r["min"] / 60) for r in outs
                   if r.get("soc_start") is not None and r.get("soc_end") is not None and r["min"] >= 60 and r["soc_start"] > r["soc_end"])
    deep = [r for r in outs if r.get("soc_end") is not None and r["soc_end"] < 25]
    if (len(rates) >= 3 or len(deep) >= 2) and rates:
        rate = rates[len(rates) // 2]
        hours = 85 / rate if rate > 0 else 99
        if (hours < 8 or len(deep) >= 2) and due("battery"):
            longest = max(r["min"] for r in outs)
            tg.staff_msg(f"💡 <b>{title}</b>: стоит предложить клиенту увеличить резерв.\n"
                         f"За 30 дней отключений: {len(outs)}, самое долгое {dur_ru(longest)}. Без сети АКБ теряет около {rate:.0f}% в час — полного заряда хватает примерно на {hours:.0f} ч."
                         + (f" {len(deep)} раз(а) батарея опускалась ниже 25%." if deep else "") +
                         f"\n{WINTER}\n<b>Что предложить:</b> дополнительный АКБ; разделить нагрузку на критичную и некритичную, чтобы батарея не кормила бойлеры и обогрев; генератор через вход GEN как запас на долгие отключения."
                         f"\n\n<b>Как посчитано:</b> взяты отключения за 30 дней длиннее часа ({len(rates)} шт.). Для каждого: (заряд в начале − заряд в конце) ÷ часы без сети. Средний расход {rate:.0f}%/ч. "
                         f"От 100% до запаса 15% это 85 ÷ {rate:.0f} ≈ {hours:.0f} ч. Порог для совета: меньше 8 ч или два и более разряда ниже 25%. "
                         "Днём панели помогают батарее, поэтому ночью и в пасмурную погоду хватит на меньшее время.")
    # 2. АКБ встречает отключения незаряженным
    lows = [r["soc_start"] for r in outs if r.get("soc_start") is not None and r["soc_start"] < 80]
    if len(lows) >= 3 and due("precharge"):
        tg.staff_msg(f"⚙️ <b>{title}</b>: батарея встречает отключения незаряженной.\n"
                     f"За 30 дней {len(lows)} отключений началось при заряде ниже 80% (в среднем {sum(lows) / len(lows):.0f}%).\n"
                     "<b>Что поменять в настройках:</b> включить заряд от сети (Grid Charge) и поднять целевой % в Time of Use, чтобы между отключениями батарея добиралась до полного. Резерв не снижать.\n" + WINTER +
                     f"\n\n<b>Как посчитано:</b> по журналу за 30 дней смотрится заряд АКБ в момент, когда пропала сеть. Значения: {', '.join(f'{x:.0f}%' for x in lows[-8:])}. Порог для совета: три и более отключения с зарядом ниже 80%.")
    # 3. инвертор на пределе
    week = (t - timedelta(days=7)).strftime("%Y-%m-%d")
    if rated and len([d for d in p.get("ovl") or [] if d >= week]) >= 3 and due("overload"):
        tg.staff_msg(f"💡 <b>{title}</b>: инвертор работает на пределе мощности.\n"
                     f"Нагрузка доходила до {p.get('ovl_max', 0) / 1000:.1f} кВт при номинале {rated / 1000:.0f} кВт, и так было в 3+ дня за неделю.\n"
                     "От сети это проходит, а при отключении света инвертор может уйти в перегрузку и выключить объект.\n"
                     "<b>Что предложить:</b> вынести мощные приборы с резервной линии или добавить второй инвертор в параллель."
                     f"\n\n<b>Как посчитано:</b> при каждой проверке нагрузка сравнивается с номиналом инвертора из Deye ({rated / 1000:.0f} кВт). Выше 90% было в дни: {', '.join(d[8:] + '.' + d[5:7] for d in p.get('ovl') or [] if d >= week)}. Порог для совета: три разных дня за неделю.")
        p["ovl_max"] = 0
    # 4. низкое напряжение сети
    if len([d for d in p.get("lowv") or [] if d >= week]) >= 3 and due("lowvolt"):
        tg.staff_msg(f"⚙️ <b>{title}</b>: в сети низкое напряжение — до {p.get('lowv_min', 0):.0f} В, в 3+ дня за неделю.\n"
                     "При такой просадке инвертор может считать сеть негодной и переходить на батарею, расходуя заряд без отключения света.\n"
                     "<b>Что проверить:</b> ввод и сечение кабеля; если просадка идёт от города — предупредить клиента и проверить допустимые пределы напряжения сети в настройках инвертора."
                     f"\n\n<b>Как посчитано:</b> при каждой проверке берётся напряжение сети по фазам с инвертора. Ниже 195 В (норма 230 В, допуск −10% это 207 В) было в дни: {', '.join(d[8:] + '.' + d[5:7] for d in p.get('lowv') or [] if d >= week)}. Порог для совета: три разных дня за неделю.")
        p["lowv_min"] = 999


def morning(tg, st, dtek, t):
    """Утренняя сводка клиенту в 7:00: что со светом сегодня и что было ночью."""
    day = t.strftime("%Y-%m-%d")
    if st.get("morning_day") == day or not (7 <= t.hour < 11):
        return
    st["morning_day"] = day
    per_chat = {}
    for sid, p in st["stations"].items():
        for chat in p.get("clients") or []:
            per_chat.setdefault(chat, []).append((sid, p))
    emerg = st["dtek"].get("emerg")
    for chat, items in per_chat.items():
        parts = []
        for sid, p in items:
            title = esc(p.get("title") or p.get("name"))
            last = p.get("last") or {}
            if p.get("offline"):
                head = f"🔴 <b>{title}</b>: станція не на зв'язку" + (f" з {since_hm(datetime.fromisoformat(p['offline_since']))}" if p.get("offline_since") else "") + "."
                cause = p.get("off_cause")
                head += {"wifi": " Схоже, причина — інтернет або Wi-Fi: перевірте роутер і логер (маленький модуль з антеною знизу інвертора).",
                         "battery": " Схоже, розрядилась батарея: станція увімкнеться сама, коли з'явиться мережа.",
                         "inverter": " Логер на зв'язку, а інвертор не відповідає — перевірте його екран і напишіть у технічну підтримку."}.get(cause, "")
            elif p.get("no_grid"):
                since = datetime.fromisoformat(p["off_since"])
                head = f"🟠 <b>{title}</b>: світла немає з {since_hm(since)} ({dur((now() - since).total_seconds() / 60)}), об'єкт працює від станції."
            else:
                head = f"🟢 <b>{title}</b>: світло є."
            if last.get("soc") is not None and not p.get("offline"):
                head += f" АКБ {last['soc']:.0f}%."
            q = queue_key(p.get("queue"))
            sl = (dtek["days"].get(day) or {}).get(q) if dtek and q else None
            if not sched_on(p, chat):
                pass  # клиент отключил графики по этому объекту
            elif sl is not None:
                for n, qq in enumerate(queues(p), 1):
                    s2 = (dtek["days"].get(day) or {}).get(qq)
                    head += (f"\nСьогодні, лінія {n}, черга " if p.get("queue2") else "\nСьогодні, черга ") + f"{qq.replace('GPV', '')}: {fmt_intervals(s2) if s2 is not None else 'графік ще не опубліковано'}"
                offs = intervals((dtek["days"].get(day) or {}).get(eff_key(dtek, sid, p, q)) or sl, 2)
                rate = usual_rate(st, sid)
                cap, ukw = capacity(st, sid, p)[0], usual_kw(st, sid)
                can = 0.85 * cap / ukw if cap and ukw else ((100 - 15) / rate if rate else None)
                if offs and can and last.get("soc") is not None:
                    longest = max(b - a for a, b in offs) / 2
                    head += (f"\nНайдовше відключення — {longest:g} год. Повного заряду за звичного споживання вистачає приблизно на {can:.0f} год"
                             + (" — має вистачити." if can >= longest + 0.5 else " — ⚠️ може не вистачити, заощаджуйте заряд."))
            elif not q:
                head += f"\nЧерга ДТЕК не вказана — натисніть «{BTN_QUEUE}», щоб отримувати графік."
            else:
                head += "\nГрафік ДТЕК на сьогодні ще не опубліковано."
            night = [r for r in st["log"] if r.get("sid") == sid and r.get("end", "") >= (t - timedelta(hours=8)).isoformat()
                     and (datetime.fromisoformat(r["end"]).hour >= 23 or datetime.fromisoformat(r["end"]).hour < 7)]
            if night:
                head += f"\nУночі світла не було: {dur(sum(r['min'] for r in night))}."
            parts.append(head)
        text = "☀️ <b>Доброго ранку!</b>\n\n" + "\n\n".join(parts)
        if emerg:
            text += "\n\n🚨 ДТЕК повідомляє про екстрені відключення: графіки можуть не діяти."
        tg.send(chat, text, KEYBOARD)


def blocked_clients(tg, st):
    """Клиент заблокировал бота или удалил чат - отключаем его и сообщаем в группу."""
    for chat in tg.blocked:
        for p in st["stations"].values():
            if chat in (p.get("clients") or []):
                p["clients"].remove(chat)
                (p.get("joins") or {}).pop(chat, None)
                who = (p.get("names") or {}).pop(chat, None) or "клиент"
                tg.staff_msg(f"🚫 {who} заблокировал бота или удалил чат с ним — уведомления по <b>{esc(p.get('title') or p.get('name'))}</b> ему больше не доходят, он отключён от станции.\n"
                             f"Если это случайно, отправьте ему ссылку заново: <code>/link {esc(p.get('name'))}</code>"
                             "\n\n<b>Как определено:</b> Telegram вернул отказ (код 403) при отправке сообщения этому клиенту.")
    tg.blocked.clear()


PV_KEYS = [f"DCPowerPV{i}" for i in range(1, 5)]


def pv_strings(tg, p, title, dev, t):
    """Сравнение стрингов панелей: каждый стринг сравнивается со своей же обычной долей в выработке."""
    today = t.strftime("%Y-%m-%d")
    pv = p.setdefault("pv", {"day": today, "sum": [0, 0, 0, 0], "n": 0})
    if pv["day"] != today:
        # день закончился - подводим итог
        if pv["n"] >= 20 and sum(pv["sum"]) > 0:
            tot = sum(pv["sum"])
            hist = p.setdefault("pv_hist", [])
            hist.append({"d": pv["day"], "share": [round(x / tot, 4) for x in pv["sum"]]})
            del hist[:-21]
            sent = p.setdefault("pv_sent", {})
            for i in range(4):
                key = f"PV{i + 1}"
                base_days = [h["share"][i] for h in hist[:-1]]
                if len(base_days) < 3:
                    continue
                base = sorted(base_days)[len(base_days) // 2]
                cur = hist[-1]["share"][i]
                last = sent.get(key)
                if base < 0.08 or (last and (t - datetime.fromisoformat(last)).days < 7):
                    continue
                d = pv["day"]
                if cur < 0.01:
                    sent[key] = t.isoformat()
                    tg.staff_msg(f"🛠 <b>{title}</b>: стринг панелей {key} не дал энергии за {d[8:]}.{d[5:7]}.\n"
                                 "<b>Возможные причины:</b> отключён или выбит автомат / разъединитель DC, сгорел предохранитель, отошёл или оплавился коннектор MC4, обрыв кабеля.\n"
                                 "<b>Что сделать:</b> проверить напряжение стринга на входе инвертора и защиту в DC-щитке."
                                 f"\n\n<b>Как посчитано:</b> обычно {key} даёт около {base * 100:.0f}% выработки станции (медиана за {len(base_days)} дн.), а за этот день — {cur * 100:.1f}%, при том что остальные стринги работали. Сравнение идёт со своей же обычной долей, поэтому разный размер и направление стрингов не мешают.")
                elif len(hist) >= 8 and all(h["share"][i] < 0.7 * base for h in hist[-3:]):
                    older = sorted(h["share"][i] for h in hist[:-3])
                    base2 = older[len(older) // 2]
                    if base2 >= 0.08 and all(h["share"][i] < 0.7 * base2 for h in hist[-3:]):
                        sent[key] = t.isoformat()
                        avg = sum(h["share"][i] for h in hist[-3:]) / 3
                        tg.staff_msg(f"🛠 <b>{title}</b>: стринг панелей {key} просел — даёт заметно меньше обычного уже 3 дня подряд.\n"
                                     "<b>Возможные причины:</b> новое затенение (дерево, постройка, снег на части панелей), сильное загрязнение, неисправная панель или диод в стринге.\n"
                                     "<b>Что сделать:</b> осмотреть панели этого стринга; если чисто и тени нет — замерить напряжение и ток стринга."
                                     f"\n\n<b>Как посчитано:</b> обычная доля {key} в выработке — {base2 * 100:.0f}% (медиана за {len(older)} дн.), за последние 3 дня — {avg * 100:.0f}%, то есть на {100 - avg / base2 * 100:.0f}% меньше. Порог: меньше 70% от обычной доли три дня подряд. "
                                     "Зимой низкое солнце даёт сезонные тени, поэтому перед выездом стоит посмотреть, не от них ли это.")
        pv = p["pv"] = {"day": today, "sum": [0, 0, 0, 0], "n": 0}
    vals = [num(dev.get(k)) or 0 for k in PV_KEYS]
    if max(vals) > 300:  # считаем только когда панели реально работают
        pv["sum"] = [round(a + b) for a, b in zip(pv["sum"], vals)]
        pv["n"] += 1


def write_fields(st, fields):
    """Список полей Deye -> файл deye_fields.json в репозитории (его кладёт туда шаг GitHub Actions).
    Станции идут под номерами в случайном порядке. Значений, названий, адресов, серийных номеров нет."""
    if not fields:
        return
    fields = sorted(fields, key=lambda x: json.dumps(x, sort_keys=True))
    out = {"updated": now().strftime("%Y-%m-%d"), "stations": len(fields),
           "note": "Только названия полей Deye и единицы измерения. Значений и данных станций здесь нет.",
           "per_station": [dict(x, station=i + 1) for i, x in enumerate(fields)]}
    h = hashlib.sha1(json.dumps(out["per_station"], sort_keys=True).encode()).hexdigest()
    with open(STATE + ".fields", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    if st.get("fields_hash") != h:  # поля изменились - записать файл в репозиторий
        st["fields_hash"] = h
        open(STATE + ".commit", "w").close()


def check(api, tg, st, dtek):
    cfgs = station_cfg()
    low_soc = float(env("LOW_SOC", "40"))
    stale = float(env("STALE_MIN", "40"))
    stable_min = float(env("STABLE_MIN", "15"))
    t = now()
    last_al = st.get("alerts_at")
    do_alerts = not last_al or (t - datetime.fromisoformat(last_al)).total_seconds() > 540
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

    fields = []
    for s in stations:
        sid = str(s.get("id") or s.get("stationId"))
        name = s.get("name") or s.get("stationName") or sid
        p = st["stations"].setdefault(sid, {})
        p["name"] = name
        p.setdefault("code", secrets.token_urlsafe(9))
        cfg = setup(st, cfgs, sid)
        title, chat, q = esc(cfg["title"]), cfg["chats"], cfg["queue"]
        q = eff_key(dtek, sid, p, q)  # две линии: общий график по схеме подключения
        try:
            lt = api.latest(int(sid))
        except Exception:
            lt = {}
        dev, units = {}, {}
        for sn in sns.get(sid, []):
            dev.update(devdata.get(sn) or {})
            units.update(api.units.get(sn) or {})
        if p.get("cap_v") != 2:  # однократно: сбросить ёмкость, принятую до защиты от скачков (Dynamica: сбой 100 А·ч вместо 300)
            for k in ("cap_deye", "cap_deye1", "cap_sig", "cap_sig_new", "cap_sig_n", "cap_rejected"):
                p.pop(k, None)
            p["cap_v"] = 2
        cap_d, cap_1, cap_sig = deye_cap_kwh(dev, units)
        soc_now = num(lt.get("batterySOC"))
        # кВт·ч = А·ч × напряжение, а напряжение гуляет с зарядом (у полной АКБ выше номинала, у пустой ниже).
        # Поэтому пересчитываем только при заряде 40-60% (напряжение около номинального)
        # или если в инверторе поменяли А·ч (добавили модули, поправили настройку).
        # Смена А·ч в Deye: принимаем, только если новое значение держится 5 проверок подряд (~10 мин) -
        # Deye иногда отдаёт неполные данные. Резкий скачок (меньше 70% или больше 150% прежней) сам не принимаем:
        # пишем в группу и считаем по прежней, пока не подтвердит факт отключений или /akb.
        changed = bool(cap_d and p.get("cap_sig") and p.get("cap_deye")) and p["cap_sig"] != cap_sig
        if changed:
            p["cap_sig_n"] = p.get("cap_sig_n", 0) + 1 if p.get("cap_sig_new") == cap_sig else 1
            p["cap_sig_new"] = cap_sig
            jump = not 0.7 <= cap_d / p["cap_deye"] <= 1.5
            if p["cap_sig_n"] >= 5 and jump and p.get("cap_rejected") != cap_sig:
                p["cap_rejected"] = cap_sig
                tg.staff_msg(f"⚠️ <b>{esc(name)}</b>: Deye вдруг показывает ёмкость АКБ ~{cap_d:.0f} кВт·ч вместо ~{p['cap_deye']:.0f}.\n"
                             f"Данные инвертора: было <code>{esc(p['cap_sig'])}</code>, стало <code>{esc(cap_sig)}</code> (А·ч).\n"
                             f"Прогнозы клиенту считаю по прежней ~{p['cap_deye']:.0f} кВт·ч. Если ёмкость правда изменилась (сняли модули) — "
                             f"<code>/akb {esc(name)} {cap_d:.0f}</code>. Если нет — ничего делать не нужно, бот сверит с фактом отключений.")
            if p["cap_sig_n"] < 5 or jump:
                cap_d = None
        else:
            for k in ("cap_sig_new", "cap_sig_n", "cap_rejected"):
                p.pop(k, None)
        if cap_d and (not p.get("cap_deye") or changed or (soc_now is not None and 40 <= soc_now <= 60 and p.get("cap_sig") == cap_sig)):
            if changed:
                tg.staff_msg(f"ℹ️ <b>{esc(name)}</b>: в инверторе изменилась ёмкость АКБ: было ~{p['cap_deye']:.0f} кВт·ч, стало ~{cap_d:.0f} кВт·ч "
                             f"(<code>{esc(p['cap_sig'])}</code> → <code>{esc(cap_sig)}</code>). "
                             "Если добавляли или меняли модули — всё верно, прогнозы уже считаются по новой.")
            p["cap_deye"], p["cap_deye1"], p["cap_sig"] = round(cap_d, 1), round(cap_1, 1), cap_sig
        # Для настройки кода: какие поля отдаёт Deye. Только названия и единицы - без значений и названий станций.
        fields.append({"inverters": len(sns.get(sid, [])),
                       "device_types": sorted({str(d.get("deviceType")) for d in s.get("deviceListItems") or []}),
                       "station_latest": sorted(k for k in lt if k not in ("requestId", "code", "msg", "success")),
                       "inverter_fields": {k: (units.get(k) or "") for k in sorted(dev) if k},
                       "capacity_found": bool(cap_d), "queues": len(queues(p))})
        status = str(s.get("connectionStatus") or "").upper()
        upd = num(lt.get("lastUpdateTime") or s.get("lastUpdateTime"))
        age = None
        if upd:
            age = (time.time() - (upd / 1000 if upd > 1e11 else upd)) / 60
        soc = num(lt.get("batterySOC"))
        load = num(lt.get("consumptionPower")) or 0

        offline = bool((status and status not in OK_STATUS) or (age is not None and age > stale))
        if offline:
            # Почему пропала связь: смотрим, что было перед этим, и отвечает ли логгер.
            devs = s.get("deviceListItems") or []
            st_of = lambda kind: [str(d.get("connectStatus", d.get("connectionStatus", ""))).upper() for d in devs
                                  if str(d.get("deviceType", "")).upper() == kind]
            on = ("1", "NORMAL", "ONLINE")
            logger_ok = any(x in on for x in st_of("COLLECTOR"))
            inv_bad = any(x and x not in on for x in st_of("INVERTER"))
            last = p.get("last_soc")
            if logger_ok and inv_bad:
                cause = "inverter"
            elif p.get("no_grid") and last is not None and last < max(low_soc, 30):
                cause = "battery"
            else:
                cause = "wifi"
            gone = f"немає даних {age:.0f} хв" if age and age > stale else "логер не відповідає"
            gone_ru = f"нет данных {age:.0f} мин" if age and age > stale else "логгер не отвечает"
            if not p.get("offline"):
                p["off_cause"], p["offline_since"] = cause, t.isoformat()
                if cause == "wifi":
                    was = ("перед цим на об'єкті не було світла, але заряд АКБ був достатній"
                           + (f" ({last:.0f}%)" if last is not None else "") if p.get("no_grid")
                           else "перед цим станція працювала нормально, мережа була")
                    tg.clients(chat, f"🔴 <b>{title}</b>: станція не на зв'язку ({gone}).\n"
                                     f"Схоже, причина — <b>інтернет або Wi-Fi</b>: {was}.\n"
                                     "Сама станція, найімовірніше, працює — ми просто перестали її бачити.\n\n"
                                     "Що перевірити:\n"
                                     "1. Чи працює інтернет на об'єкті.\n"
                                     "2. Чи увімкнений роутер. Коли вимикають світло, роутер має живитися від станції.\n"
                                     "3. Чи не змінювали пароль або назву Wi-Fi, чи не міняли роутер.\n"
                                     "4. Логер — маленький модуль з антеною знизу інвертора. Вийміть його на 10 секунд і вставте назад.\n\n"
                                     "Якщо світло в приміщенні є — хвилюватися не варто. Не допомогло — напишіть у технічну підтримку." + support(st))
                elif cause == "battery":
                    tg.clients(chat, f"🔴 <b>{title}</b>: станція не на зв'язку ({gone}).\n"
                                     f"Схоже, причина <b>не у Wi-Fi</b>: перед цим на об'єкті не було світла, а заряд АКБ був лише {last:.0f}%.\n"
                                     "Імовірно, батарея розрядилася і станція вимкнулась. Вона увімкнеться сама, коли з'явиться мережа.\n"
                                     "Якщо світло вже дали, а станція не запрацювала — напишіть у технічну підтримку." + support(st))
                else:
                    tg.clients(chat, f"🔴 <b>{title}</b>: інвертор не відповідає ({gone}).\n"
                                     "Причина <b>не у Wi-Fi</b>: логер на зв'язку, інтернет працює, а даних від інвертора немає.\n"
                                     "Перевірте, чи увімкнений інвертор і чи немає помилки на його екрані, та напишіть у технічну підтримку." + support(st))
                    tg.staff_msg(f"🔴 <b>{title}</b>: инвертор не отвечает ({gone_ru}), при этом логгер на связи — это не Wi-Fi. Свяжитесь с клиентом."
                                 "\n\n<b>Как определено:</b> Deye Cloud показывает логгер «на связи», а инвертор «не на связи». Значит интернет на объекте есть, а данные не отдаёт сам инвертор: выключен, в аварии или потеряна связь логгера с ним.")
                p["off_staff"] = cause == "inverter"  # Wi-Fi и севший АКБ - только клиенту
                if tg.quiet():
                    p["night_off"] = True  # ночью не будим: если к 7:00 связь не вернётся, скажем в утренней сводке
            elif not p.get("off_staff") and p.get("offline_since") and \
                    (t - datetime.fromisoformat(p["offline_since"])).total_seconds() > 24 * 3600:
                p["off_staff"] = True
                tg.staff_msg(f"🔴 <b>{title}</b>: станция не на связи уже больше суток. " + ("Перед этим сел АКБ, и станция сама не включилась." if p.get("off_cause") == "battery" else "Сначала это было похоже на интернет / Wi-Fi, но клиент, видимо, не починил.") + " Стоит позвонить."
                              f"\n\n<b>Как определено:</b> последние данные от станции были {datetime.fromisoformat(p['offline_since']).strftime('%d.%m около %H:%M')}"
                              + (f", последний известный заряд АКБ {p['last_soc']:.0f}%" if p.get("last_soc") is not None else "")
                              + (", сети в тот момент не было." if p.get("no_grid") else ", сеть в тот момент была."))
        if p.get("offline") and not offline:
            if not p.pop("night_off", None):
                tg.clients(chat, f"🟢 <b>{title}</b>: станція знову на зв'язку.")
            if p.get("off_staff"):
                tg.staff_msg(f"🟢 <b>{title}</b>: снова на связи.")
            for k in ("off_cause", "offline_since", "off_staff"):
                p.pop(k, None)
        p["offline"] = offline
        if offline:
            continue  # пока нет связи, про сеть и АКБ ничего утверждать нельзя
        if soc is not None:
            p["last_soc"] = soc
        p.pop("last", None)
        apply_latest(p, lt, t)
        dkw = p["last"].get("dkw")

        no_grid, how = grid_state(lt, dev)
        p["how"] = how
        if no_grid is True:
            p["off_n"] = p.get("off_n", 0) + 1
            need = 1 if how == "voltage" else 2  # по мощности - только после 2 проверок подряд
            if p.get("no_grid") and p.get("on_since"):
                # свет дали и снова забрали - это то же отключение, отдельно не пишем
                p.pop("on_since", None)
                p["flaps"] = p.get("flaps", 0) + 1
            if not p.get("no_grid") and p["off_n"] >= need:
                p["no_grid"] = True
                p["off_since"] = t.isoformat()
                p["off_soc"] = soc
                p["off_kwh"], p["off_t"] = 0.0, t.isoformat()
                p["off_snap"] = snapshot(dtek, t)  # график всех очередей на момент отключения
                p["off_emerg"] = bool(st["dtek"].get("emerg"))
                plan = planned_now(dtek, q, t)
                p["off_planned"] = plan
                if plan is True:
                    kind = "за графіком ДТЕК"
                elif plan is False:
                    kind = "⚡ <b>поза графіком</b> — схоже на екстрене або аварійне відключення"
                else:
                    kind = "чергу ДТЕК для об'єкта не вказано" if not q else "графік ДТЕК зараз недоступний"
                bat = f"\nАКБ {soc:.0f}%, навантаження {load / 1000:.1f} кВт." if soc is not None else f"\nНавантаження {load / 1000:.1f} кВт."
                until = planned_end(dtek, q, t) if plan is True else None
                p["off_until"] = until.isoformat() if until else None
                fore = forecast_line(st, sid, soc, t, until, p, dkw)
                tg.clients(chat, f"🟠 <b>{title}</b>: світло вимкнули о ~{hm(t)}.\n{kind[0].upper() + kind[1:] if kind[0] != chr(9889) else kind}.{bat}{fore}\nОб'єкт працює від станції.", urgent=True)
        elif no_grid is False:
            p["off_n"] = 0
            if p.get("no_grid"):
                # Пишем «увімкнули» один раз, когда сеть держится стабильно, а не на каждое моргание.
                p.setdefault("on_since", t.isoformat())
                back = datetime.fromisoformat(p["on_since"])
                if (t - back).total_seconds() / 60 >= stable_min:
                    start = datetime.fromisoformat(p["off_since"])
                    mins = (back - start).total_seconds() / 60
                    st["log"].append({"sid": sid, "name": name, "start": p["off_since"], "end": p["on_since"],
                                      "min": round(mins), "planned": p.get("off_planned"), "flaps": p.get("flaps", 0),
                                      "soc_start": p.get("off_soc"), "soc_end": p.get("on_soc", soc),
                                      "kwh": round(p["off_kwh"], 1) if p.get("off_kwh") else None,
                                      "emerg": bool(p.get("off_emerg")),
                                      "m": sorted(k for k, ok in matches(p.get("off_snap"), start, back,
                                                  pairs=[tuple(queues(p))] if len(queues(p)) == 2 else ()).items() if ok)
                                           if p.get("off_snap") else None})
                    bat = ""
                    if p.get("on_soc", soc) is not None and p.get("off_soc") is not None:
                        bat = f"\nАКБ: було {p['off_soc']:.0f}%, найменше {min(p.get('min_soc', 100), p.get('on_soc', soc)):.0f}%, зараз {soc:.0f}%." if soc is not None else ""
                    fl = f"\nЗа цей час світло з'являлось і знову зникало: {p['flaps']} раз(и)." if p.get("flaps") else ""
                    tg.clients(chat, f"🟢 <b>{title}</b>: світло увімкнули о ~{hm(back)}, мережа стабільна.\nБез мережі: {dur(mins)}.{fl}{bat}", urgent=True)
                    for k in ("no_grid", "off_since", "off_soc", "off_planned", "on_since", "on_soc", "flaps", "min_soc", "off_until", "off_kwh", "off_t", "off_snap", "off_emerg"):
                        p.pop(k, None)
                elif "on_soc" not in p and soc is not None:
                    p["on_soc"] = soc
        if p.get("no_grid") and soc is not None:
            p["min_soc"] = min(p.get("min_soc", 100), soc)
        # Сколько кВт·ч отдала АКБ за отключение: по ним бот сам узнаёт фактическую ёмкость.
        if no_grid is True and p.get("no_grid") and p.get("off_t") and dkw is not None:
            dt = (t - datetime.fromisoformat(p["off_t"])).total_seconds() / 3600
            if 0 < dt < 0.5:  # большой разрыв (не было связи) не считаем
                p["off_kwh"] = p.get("off_kwh", 0) + dkw * dt
            p["off_t"] = t.isoformat()
        if p.get("no_grid") and st["dtek"].get("emerg"):
            p["off_emerg"] = True  # во время отключения ДТЭК объявил экстренные - для проверки очереди не считаем
        cap_warn(tg, st, sid, p, t)
        queue_review(tg, st, sid, p, t)

        low = soc is not None and soc < low_soc
        if low and not p.get("low_soc") and p.get("no_grid") and not p.get("on_since"):
            tg.clients(chat, f"🔋 <b>{title}</b>: заряд АКБ {soc:.0f}%, світла досі немає.\nВарто вимкнути потужні прилади (бойлер, кондиціонер, плиту), щоб вистачило довше.")
        p["low_soc"] = bool(low)

        # ---- дальше только то, что нужно сотрудникам: неисправности и рекомендации
        problems(tg, st, p, title, dev, soc, load, low_soc, t, no_grid)
        if do_alerts:
            try:
                device_alerts(tg, p, title, api.alerts(int(sid), time.time() - 86400, time.time() + 600), t)
            except Exception:
                pass
        advice(tg, st, sid, p, title, dev, load, t)
        pv_strings(tg, p, title, dev, t)

    if do_alerts:
        st["alerts_at"] = t.isoformat()
    firmware_mismatch(tg, st)
    write_fields(st, fields)
    # клиенты, которые подключились, но за 30 минут так и не прислали очередь
    for sid, p in st["stations"].items():
        for chat_id, j in list((p.get("joins") or {}).items()):
            if (t - datetime.fromisoformat(j["at"])).total_seconds() > 1800:
                head = f"👤 {j['who']} подключился к <b>{esc(p.get('title') or p.get('name'))}</b>."
                if queues(p):  # очередь за это время поставил другой клиент или сотрудник
                    tg.staff_msg(f"{head} Очередь ДТЭК {' и '.join(q.replace('GPV', '') for q in queues(p))}.")
                else:
                    tg.send(tg.staff, f"{head} Очередь ДТЭК не указана ни им, ни другими клиентами. Выберите:", queue_grid(f"q:{sid}:"))
                p["joins"].pop(chat_id)
    morning(tg, st, dtek, t)
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
        p = st["stations"][sid]
        all_chats += c["chats"]  # экстренные - всем, даже кто отключил графики
        want = [ch for ch in c["chats"] if sched_on(p, ch)]
        q2 = queue_key(p.get("queue2"))
        for q, n in ((c["queue"], c["title"]), (q2, f"{c['title']} (лінія 2)")):
            if q and want:
                by_q.setdefault(q, []).append((n, want))

    # 1. общий текст ДТЭК: экстренные / аварийные
    att = dtek["attention"]
    h = hashlib.sha1(att.encode("utf-8")).hexdigest()
    if h != D.get("att_hash"):
        emerg = is_emergency(att)
        if D.get("att_hash") is not None or emerg:
            if emerg:
                msg = "🚨 <b>ДТЕК Одеса: екстрені / аварійні відключення</b>\nГрафіки зараз можуть не діяти, світло можуть вимкнути без попередження.\n\n" + esc(att[:700])
                tg.clients(all_chats, msg)
            elif D.get("emerg"):
                tg.clients(all_chats, "✅ <b>ДТЕК Одеса</b>: повідомлення про екстрені відключення знято. Графіки знову діють.")
        D["att_hash"], D["emerg"] = h, emerg

    # 2. графики по очередям: новый / изменился
    seen = D.setdefault("sched", {})
    for day, label in ((today, "сьогодні"), (tomorrow, "завтра")):
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
            head = "змінився графік" if changed else "графік"
            qn = q.replace("GPV", "")
            body = f"черга {qn}, {label} ({day[8:]}.{day[5:7]}):\n{fmt_intervals(sl)}"
            for n, chats in people:
                tg.clients(chats, f"🗓 <b>{esc(n)}</b>: {head} відключень ДТЕК, {body}")
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
                    txt = f"⏰ <b>{esc(n)}</b>: за графіком ДТЕК світло вимкнуть о {slot_time(a)} (через ~{left:.0f} хв), до {slot_time(b)}."
                    tg.clients(chat, txt)
    for k in [k for k in warned if k.split("|")[0] < today]:
        warned.pop(k)
    D["started"] = True


# ---------------------------------------------------------------- отчёт за месяц
MONTHS = ["", "січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]


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
        lines = [f"📊 <b>{title}</b> — звіт за {MONTHS[m]} {y}"]
        try:
            rows = [r for r in api.history(int(sid), 3, ym, ym) if int(num(r.get("month")) or m) == m] or []
            r = rows[0] if rows else {}
            g, cns, buy = num(r.get("generationValue")), num(r.get("consumptionValue")), num(r.get("purchaseValue"))
            if g is not None:
                lines.append(f"☀️ Панелі виробили: {g:,.0f} кВт·год".replace(",", " "))
            if cns is not None:
                lines.append(f"🏠 Об'єкт спожив: {cns:,.0f} кВт·год".replace(",", " "))
            if buy is not None:
                lines.append(f"🔌 Куплено з мережі: {buy:,.0f} кВт·год".replace(",", " "))
            if cns and buy is not None:
                lines.append(f"✅ Власною енергією покрито: {max(0, (1 - buy / cns) * 100):.0f}%")
        except Exception:
            lines.append("Даних про виробіток за місяць Deye Cloud не надав.")
        out = [r for r in st["log"] if r.get("sid") == sid and r.get("start", "")[:7] == ym]
        if out:
            total = sum(r["min"] for r in out)
            unpl = [r for r in out if r.get("planned") is False]
            lines.append(f"\n💡 Відключень світла: {len(out)}, разом {dur(total)}.")
            lines.append(f"Найдовше: {dur(max(r['min'] for r in out))}.")
            if unpl:
                lines.append(f"Поза графіком (екстрені/аварійні): {len(unpl)}, {dur(sum(r['min'] for r in unpl))}.")
            lines.append("Увесь цей час об'єкт працював від станції.")
        else:
            lines.append("\n💡 Відключень світла за місяць програма не зафіксувала.")
        tg.clients(c["chats"], "\n".join(lines))
        n += 1
    return n


def peek(api, st):
    """Отчёт для Claude (с разрешения владельца, 24.11.2026): что бот видит в Deye и его память по станциям.
    Без чатов и имён клиентов. Шифруется открытым ключом claude_pub.pem (шаг GitHub Actions) -
    прочитать может только владелец закрытого ключа. Запускается только вручную: режим peek."""
    api.login()
    out = {"at": now().isoformat(), "stations": []}
    for s in api.stations_with_devices():
        sid = str(s.get("id") or s.get("stationId"))
        sns = [str(d.get("deviceSn")) for d in s.get("deviceListItems") or [] if str(d.get("deviceType", "")).upper() == "INVERTER"]
        try:
            dev = api.device_latest(sns) if sns else {}
        except Exception as e:
            dev = {"error": str(e)[-80:]}
        try:
            lt = api.latest(int(sid))
        except Exception as e:
            lt = {"error": str(e)[-80:]}
        p = st["stations"].get(sid, {})
        out["stations"].append({"sid": sid, "name": p.get("name"), "inverters": dev, "latest": lt,
                                "units": {sn: api.units.get(sn) for sn in sns},
                                "memory": {k: v for k, v in p.items() if k not in ("clients", "names", "joins", "code", "nosched", "off_snap")},
                                "log": [x for x in st["log"] if x.get("sid") == sid][-10:]})
    with open(STATE + ".peek", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, default=str)
    open(STATE + ".commit", "w").close()
    print("peek: станций", len(out["stations"]))


def probe(api):
    """Разведка: какие организации есть у аккаунта и сколько станций видно с каждым входом. В журнал - только числа."""
    api.login()
    print("PROBE personal stations:", len(api.stations()))
    try:
        info = api._post("/account/info", {})
        orgs = info.get("orgInfoList") or []
        print("PROBE info keys:", sorted(k for k in info if k not in ("requestId",)), "| orgs:", len(orgs))
        for o in orgs:
            print("PROBE org fields:", sorted(o))
            try:
                pw = hashlib.sha256(env("DEYE_PASSWORD").encode("utf-8")).hexdigest()
                d = api._post("/account/token", {"appSecret": env("DEYE_APP_SECRET"), "email": env("DEYE_EMAIL"), "password": pw,
                                                 "companyId": o.get("companyId")}, auth=False, query="?appId=" + env("DEYE_APP_ID"))
                tok = d.get("accessToken") or (d.get("data") or {}).get("accessToken")
                old, api.token = api.token, tok
                print("PROBE org role:", o.get("roleName"), "| stations with org login:", len(api.stations()))
                api.token = old
            except Exception as e:
                print("PROBE org login error:", str(e)[-80:])
    except Exception as e:
        print("PROBE account/info error:", str(e)[-80:])


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
        # АКБ: какие поля отдаёт Deye (только в группу сотрудников, не в журнал)
        units = {}
        for x in sn:
            units.update(api.units.get(x) or {})
        bat = [f"{k}={v}{(' ' + units[k]) if units.get(k) else ''}" for k, v in sorted(dev.items())
               if k and re.search(r"batt|bms|capacit|soh", k, re.I)][:20]
        p = st["stations"][sid]
        cap, src = capacity(st, sid, p)
        dkw = discharge_kw(lt)
        lines.append(f"АКБ: разряд {'—' if dkw is None else f'{dkw:.1f}'} кВт (dischargePower {kw(lt.get('dischargePower'))}, chargePower {kw(lt.get('chargePower'))}, "
                     f"солнце {kw(lt.get('generationPower'))}) · ёмкость: {f'{cap:.0f} кВт·ч ({src})' if cap else 'неизвестна'}"
                     f" · данные Deye на {hm(deye_time(lt)) if deye_time(lt) else '—'}"
                     + (f"\nполя АКБ: <code>{esc(', '.join(bat))}</code>" if bat else "\nполей АКБ в данных инвертора нет"))
    if dtek:
        lines.append(f"\nДТЭК: график обновлён {esc(dtek['updated'])}, дни: {', '.join(sorted(dtek['days']))}")
    else:
        lines.append("\nДТЭК: график сейчас недоступен")
    msg = ""  # длинная диагностика - несколькими сообщениями, по станциям
    for ln in lines:
        if msg and len(msg) + len(ln) > 3800:
            tg.staff_msg(msg)
            msg = ""
        msg += ("\n" if msg else "") + ln
    if msg:
        tg.staff_msg(msg)
    # в журнал - только технические названия полей, без станций и значений
    print("DIAG mode:", mode)
    # разведка: какие оповещения об ошибках отдаёт Deye (только названия полей и коды)
    end = int(time.time()); start = end - 30 * 86400
    for s in stations[:5]:
        sid = int(s.get("id") or s.get("stationId"))
        sn = [str(d.get("deviceSn")) for d in s.get("deviceListItems") or [] if str(d.get("deviceType", "")).upper() == "INVERTER"]
        for path, body in (("/station/alertList", {"stationId": sid, "startTimestamp": start, "endTimestamp": end, "page": 1, "size": 30}),
                           ("/device/alertList", {"deviceSn": (sn or [""])[0], "startTimestamp": start, "endTimestamp": end, "page": 1, "size": 30})):
            try:
                d = api._post(path, body)
                lst = next((v for v in d.values() if isinstance(v, list)), [])
                print("DIAG alerts", path, "keys:", sorted(k for k in d if k not in ("requestId",)), "| n:", len(lst), "| item keys:", sorted(lst[0]) if lst else [])
                for it in lst[:12]:
                    print("DIAG alert item:", {k: v for k, v in it.items() if k not in ("deviceSn", "stationId", "deviceId", "stationName", "deviceName")})
            except Exception as e:
                print("DIAG alerts", path, "error:", str(e)[-60:])
    for s in stations[:5]:
        sn = [str(d.get("deviceSn")) for d in s.get("deviceListItems") or [] if str(d.get("deviceType", "")).upper() == "INVERTER"]
        try:
            for v in (api.device_latest(sn) if sn else {}).values():
                print("DIAG fw/state:", {k: v.get(k) for k in ("HMI", "MAIN", "ProtocolVersion", "RatedPower", "GridStatus", "GridRelayStatus", "Battery2Status", "LithiumBattery2SOH", "Temperature- Battery", "BMSSOC", "SOC", "TotalGridPower", "GenVoltage") if k in v})
        except Exception:
            pass
    print("DIAG device types:", sorted(types))
    print("DIAG device item fields:", sorted({k for s in stations for d in s.get("deviceListItems") or [] for k in d}))
    print("DIAG device statuses:", sorted({str(d.get("connectStatus", d.get("connectionStatus"))) for s in stations for d in s.get("deviceListItems") or []}))
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
        st["dtek"].pop("fail_since", None)
        st["dtek"].pop("fail_sent", None)
    except Exception:
        dtek = None
        st["dtek"]["fail"] = st["dtek"].get("fail", 0) + 1
        st["dtek"].setdefault("fail_since", now().isoformat())
        if not st["dtek"].get("fail_sent") and (now() - datetime.fromisoformat(st["dtek"]["fail_since"])).total_seconds() > 3600:
            st["dtek"]["fail_sent"] = True
            tg.staff_msg("⚠️ Графики ДТЭК не загружаются уже час. Отключения по станциям по-прежнему отслеживаются.")
    api = Deye()
    if mode == "cmd":
        commands(tg, st, dtek, api)
        print("Команды обработаны, сообщений:", tg.sent)
        # Пока клиенты нажимают кнопки, таймер пропускает плановые проверки (запуск уже идёт).
        # Поэтому запуск для команд сам делает проверку, если прошлая была больше 100 секунд назад.
        la = st.get("last_check_at")
        if not la or (now() - datetime.fromisoformat(la)).total_seconds() > 100:
            n = check(api, tg, st, dtek)
            dtek_alerts(tg, st, dtek)
            st["last_check"], st["last_check_at"] = now().strftime("%Y-%m-%d %H:%M"), now().isoformat()
            print(f"Заодно проверено станций: {n}")
    elif mode == "help":
        tg.staff_msg(HELP)
    elif mode == "setup":
        bot_setup(tg, st)
    elif mode == "tokenwarn":
        day = now().strftime("%Y-%m-%d")
        if st.get("tokenwarn") != day:
            st["tokenwarn"] = day
            tg.staff_msg("🔑 Токен GitHub для быстрых команд и таймера скоро истечёт (осталось меньше 14 дней).\n"
                         "Когда истечёт, бот перестанет отвечать на команды, а проверки станций остановятся.\n"
                         "Что сделать: GitHub → Settings → Developer settings → Fine-grained tokens → solaris-webhook → Regenerate token, новый вставить в Vercel (проект solaris-webhook → Environment Variables → GITHUB_TOKEN) и нажать Redeploy.")
    elif mode in ("webhook", "webhook-off"):
        url = ""
        try:
            url = open(os.path.join(HERE, "webhook_url.txt"), encoding="utf-8").read().strip()
        except Exception:
            pass
        if mode == "webhook" and url and env("INBOX_KEY"):
            ok = tg.api("setWebhook", url=url, secret_token=hook_secret(),
                        allowed_updates=json.dumps(["message", "callback_query"]))
            st.setdefault("tg", {})["webhook"] = bool(ok)
            if not ok:
                tg.staff_msg("⚠️ Быстрые команды включить не получилось.")
        else:
            tg.api("deleteWebhook")
            st.setdefault("tg", {})["webhook"] = False
            tg.staff_msg("Быстрые команды выключены: бот снова отвечает при очередной проверке.")
        print("webhook:", st["tg"]["webhook"])
    elif mode == "peek":
        peek(api, st)
    elif mode == "probe":
        probe(api)
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
        commands(tg, st, dtek, api)
        n = check(api, tg, st, dtek)
        dtek_alerts(tg, st, dtek)
        st["last_check"], st["last_check_at"] = now().strftime("%Y-%m-%d %H:%M"), now().isoformat()
        print(f"Проверено станций: {n}, сообщений: {tg.sent}, ДТЭК: {'ок' if dtek else 'нет'}")
    blocked_clients(tg, st)
    # Память сохраняется в кэш каждый запуск, а в репозиторий - только когда изменилось важное
    # (клиенты, очереди, отключения, отправленные сообщения) или раз в 6 часов как резервная копия.
    def durable(x):
        x = json.loads(json.dumps(x, sort_keys=True, ensure_ascii=False))
        for k in ("last_check", "last_check_at", "alerts_at", "committed_at"):
            x.pop(k, None)
        x.get("tg", {}).pop("done", None)
        x.get("tg", {}).pop("offset", None)
        for k in ("fail", "fail_since"):
            x.get("dtek", {}).pop(k, None)
        for q in x.get("stations", {}).values():
            for k in ("last", "last_soc", "pv", "off_n", "min_soc", "on_soc", "lowv_min", "ovl_max", "nosoc", "how", "off_kwh", "off_t", "cap_deye", "cap_deye1", "cap_sig", "cap_sig_new", "cap_sig_n", "cap_rejected"):
                q.pop(k, None)
        return x

    last_commit = st.get("committed_at")
    stale = not last_commit or (now() - datetime.fromisoformat(last_commit)).total_seconds() > 6 * 3600
    if durable(json.loads(before)) != durable(st) or stale or os.path.exists(STATE + ".changed"):
        st["committed_at"] = now().isoformat()
        open(STATE + ".commit", "w").close()
    save_state(st)


if __name__ == "__main__":
    main()
