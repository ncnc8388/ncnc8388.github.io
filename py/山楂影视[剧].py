# coding=utf-8
# ==========================================================
# 山楂4K · 狼壳（TVBox 分支）PY 源  v2
# ----------------------------------------------------------
# 站点：http://43.248.128.122:578
#   不是苹果CMS采集接口，是 Expo/RN 壳 App 的 PHP 后端。
# 链路：登录换 JWT → /api/v1/security/challenge 取挑战
#       → /api/v1/security/init 换 session
#       → /api/v1/search 出列表/搜索
#       → /api/v1/video/detail 出详情（每一集带一张 tk 票根）
#       → /api/v1/vod/ticket 换票 → 票根里的解析接口拿直链
# 导入：单条放进 /sdcard/PY源/，不配 json、不装环境、不跑命令。
#
# v2 相对 v1 的改动（2026-11-09 用户反馈真机只出两条线路）：
#   1. 详情页不再做任何线路体检/隐藏：站点返回几条就列几条，
#      播不了的线路保留在列表里，只在播放日志里写明原因（不静默丢线）。
#   2. 去掉 v1 里「详情阶段串行去两家采集站搜片」的逻辑——那一步在移动网络下
#      会占掉详情预算，也是线路数量跟站点对不上的主要嫌疑之一。
#   3. 详情只发一个 HTTP 请求，单请求 8 秒超时，整体远低于壳的 40 秒预算。
#   4. 播放令牌改成 base64url(JSON)，彻底避开 $ # | 这些分隔符被壳误切的风险。
#   5. 播放仍然强制验：四字符码必须落在 hvc1/hev1/avc1/avc3、必须有视频轨、
#      中段 Range 必须回 206 才交给播放器；某条线路不达标就换下一条并打日志，
#      绝不用低清顶替、也不静默降档。
# ==========================================================
import html as _html
import json
import time
import base64
import random
import re
from urllib.parse import quote, urljoin

import requests

# ---------------- 站点常量 ----------------
BASE = "http://43.248.128.122:578"
UA = "Mozilla/5.0 (Linux; Android 12) okhttp/3.12.1"
REF = "https://www.google.com/"
USER = "sukeke"
PASS = "sukeke.1"
# 账号已绑定的设备号（换账号要改成自己的，否则会报设备数量上限）
DID = "de94cd65-a825-4972-adf9-7dbece13e9f1"

TIMEOUT = 8          # 单请求超时（秒）
SESS_TTL = 480       # 会话 8 分钟主动换一次（服务端 600 秒过期）
VER = "20261123"

# 站点真实分类（/api/v1/search?type=xx）
CATS = [
    (1, "电影"),
    (2, "连续剧"),
    (3, "综艺"),
    (4, "动漫"),
    (21, "少儿"),
    (23, "纪录片"),
]

# 这几个 CDN 带了 UA/Referer 反而回 403（2026-11-09 实测芒果 titan.mgtv.com：
# 裸请求 200 + application/vnd.apple.mpegurl，带任何 UA 或 mgtv Referer 一律 403）
NOHDR_HOSTS = ("titan.mgtv.com", "mgtv.com", "mgtvstatic.com")

# 播放器只认这几种视频四字符码
VIDEO_FOURCC = (b"hvc1", b"hev1", b"avc1", b"avc3", b"hvc2", b"hev2")

# 已知播不了的线路：只在播放日志里说明原因，不从详情页删掉（用户要求不隐藏）
KNOWN_BAD = {
    "qq": "小腾4K 的解析站回 403「账户已欠费」",
    "vod_1080": "1080P-3 的解析站给的是图片占位播放列表",
    "zy1080_01": "1080P 的解析站轮换到 CDN 403",
}

# 推广信息过滤（源内不留群号/公众号/引流）
_PROMO = [
    re.compile(r"(tg|telegram)\s*[:：]?\s*@[\w\.\-]+", re.I),
    re.compile(r"(?<![a-zA-Z])(qq|微信|vx|v信)\s*群?\s*[:：]?\s*[0-9]{5,}", re.I),
    re.compile(r"公众号\s*[:：]?\s*[^\s，,。；;]{1,20}"),
    re.compile(r"(如需|想要|需要)[^。\n]{0,12}(联系|加|购买|续费)[^。\n]{0,24}", re.I),
    re.compile(r"[a-zA-Z0-9\-_]{2,}\.(top|cc|xyz|club|vip|com|cn|net)(/[\w\-./?=&%]*)?", re.I),
]


def _clean(text):
    """清掉简介里的推广信息与 HTML 标签"""
    s = re.sub(r"<[^>]+>", "", str(text or ""))
    for p in _PROMO:
        s = p.sub("", s)
    return re.sub(r"\s+", " ", s.replace("\u3000", " ")).strip()


def _pic(u):
    """封面地址归一化。

    站点这批条目的 vod_pic 有两种脏数据（2026-11-09 实测）：
      1. 被 HTML 转义了 3 层：`...?a=1&amp;amp;amp;b=2`，直接请求会 400；
         循环反转义到稳定即可，正常返回 206 + image/webp。
      2. 完全缺失（短剧 type=20 整批没有 vod_pic 字段），只能留空由壳画占位。
    另外补 // 开头的协议相对地址。
    """
    s = str(u or "").strip()
    if not s:
        return ""
    for _ in range(5):
        u2 = _html.unescape(s)
        if u2 == s:
            break
        s = u2
    s = s.strip()
    if s.startswith("//"):
        s = "https:" + s
    if not re.match(r"^https?://", s, re.I):
        return ""
    # 空格是非法字符，直接请求会 400
    return s.replace(" ", "%20")


def _nohdr(url):
    """该域名要不要刻意不带 UA/Referer（带了必 403 的那批 CDN）"""
    u = str(url or "").lower()
    return any(h in u for h in NOHDR_HOSTS)


def _b64u_dec(seg):
    t = str(seg or "").replace("-", "+").replace("_", "/")
    while len(t) % 4:
        t += "="
    return base64.b64decode(t)


def _b64u_enc(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class Spider(object):
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": REF,
            "Accept-Encoding": "identity",
        })
        self._jwt = ""
        self._sid = ""
        self._stamp = 0.0
        self._detail_cache = {}

    # ---------------- 壳要的固定接口 ----------------
    def getName(self):
        return "山楂4K"

    def init(self, extend=""):
        try:
            self._session()
        except Exception as e:
            print("[sz] init", e)

    def isVideoFormat(self, url):
        u = str(url or "").lower()
        return any(x in u for x in (".m3u8", ".mp4", ".flv"))

    def manualVideoCheck(self):
        return False

    def getDependence(self):
        return ["requests"]

    def isVip(self):
        return False

    def getHeaders(self):
        return {"User-Agent": UA, "Referer": REF}

    # ---------------- 会话（登录 + 挑战 + 握手）----------------
    def _session(self, force=False):
        now = time.time()
        if not force and self._jwt and self._sid and (now - self._stamp) < SESS_TTL:
            return
        jwt = ""
        if self._jwt and not force and (now - self._stamp) < 3000:
            jwt = self._jwt
        if not jwt:
            r = self._post("/api/v1/user/login",
                           {"Username": USER, "Password": PASS, "device_id": DID},
                           auth=False)
            if not r or r.get("code") != 1:
                raise RuntimeError("登录失败：" + str((r or {}).get("msg") or "无响应"))
            jwt = str((r.get("data") or {}).get("token") or "")
            if not jwt:
                raise RuntimeError("登录未返回 token")
        ch = self._get("/api/v1/security/challenge", auth=False)
        if not ch or ch.get("code") != 1:
            raise RuntimeError("安全挑战失败：" + str((ch or {}).get("msg") or "无响应"))
        cd = ch.get("data") or {}
        payload = {
            "challenge": cd.get("challenge"),
            "challenge_id": cd.get("challenge_id"),
            "device_id": DID,
            "token": jwt,
            "client_token": "",
            "timestamp": int(time.time()),
            "nonce": "".join(random.choice("0123456789abcdef") for _ in range(32)),
            "username": USER,
            "password": PASS,
        }
        # 服务端只校验这些字段「存在」
        payload.update({
            "sign": "s", "signature": "s", "app_id": "1", "app_key": "1",
            "app_version": "1.0", "version": "1", "platform": "android",
            "device": "android", "version_code": 1, "uid": "1",
            "source_id": "1", "site_id": "1", "type": "1",
        })
        r2 = self._post("/api/v1/security/init", payload, auth=False)
        if not r2 or r2.get("code") != 1:
            raise RuntimeError("握手失败：" + str((r2 or {}).get("msg") or "无响应"))
        sid = str((r2.get("data") or {}).get("session_id") or "")
        if not sid:
            raise RuntimeError("握手未返回 session")
        self._jwt, self._sid, self._stamp = jwt, sid, time.time()

    def _headers(self, extra=None, auth=True):
        h = {"User-Agent": UA, "Referer": REF}
        if auth and self._jwt:
            h["Authorization"] = "Bearer " + self._jwt
        if auth and self._sid:
            h["X-Session-Id"] = self._sid
        if extra:
            h.update(extra)
        return h

    def _get(self, path, qs="", auth=True, timeout=TIMEOUT):
        url = BASE + path
        if qs:
            url += ("&" if "?" in path else "?") + qs
        try:
            r = self.s.get(url, headers=self._headers(auth=auth), timeout=timeout)
            return self._json(r)
        except Exception as e:
            raise RuntimeError(self._errmsg(e))

    def _post(self, path, obj, auth=True, timeout=TIMEOUT):
        try:
            r = self.s.post(BASE + path,
                            data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                            headers=self._headers({"Content-Type": "application/json"}, auth=auth),
                            timeout=timeout)
            return self._json(r)
        except Exception as e:
            raise RuntimeError(self._errmsg(e))

    @staticmethod
    def _json(resp):
        try:
            txt = resp.text or ""
        except Exception:
            return None
        i, j = txt.find("{"), txt.find("[")
        if i < 0 and j < 0:
            return None
        txt = txt[j:] if (i < 0 or (j >= 0 and j < i)) else txt[i:]
        try:
            return json.loads(txt)
        except Exception:
            return None

    @staticmethod
    def _errmsg(e):
        m = str(getattr(e, "message", "") or e or "")
        if re.search(r"UnknownHost|Name or service|nodename", m, re.I):
            return "域名解析失败，检查网络"
        if re.search(r"timed out|timeout|Timeout", m, re.I):
            return "接口超时，稍后重试"
        if re.search(r"Connection refused|ECONNREFUSED", m, re.I):
            return "服务器拒绝连接"
        return m[:80] or "未知错误"

    def _api(self, path, qs="", tries=2):
        """统一请求：会话失效自动重建一次；限流退避"""
        last = ""
        for i in range(tries):
            try:
                if i > 0 and re.search(r"登录|握手|session|401|授权|token", last, re.I):
                    self._session(force=True)
                else:
                    self._session()
                r = self._get(path, qs)
                if r and r.get("code") == 1:
                    return r
                last = str((r or {}).get("msg") or "空响应")
                if re.search(r"频繁|rate|稍后", last, re.I) and i < tries - 1:
                    time.sleep(2.0)
                    continue
                if re.search(r"登录|token|session|未授权|授权", last, re.I) and i < tries - 1:
                    self._session(force=True)
                    continue
                break
            except Exception as e:
                last = self._errmsg(e)
                if i < tries - 1:
                    try:
                        self._session(force=True)
                    except Exception:
                        pass
        raise RuntimeError(last or "请求失败")

    # ---------------- 列表 / 搜索 ----------------
    @staticmethod
    def _vod(it, tid_hint=""):
        vid = str(it.get("vod_id") or "")
        name = str(it.get("vod_name") or "")
        if not vid or not name:
            return None
        pic = _pic(it.get("vod_pic") or it.get("vod_pic_slide") or "")
        # 站点把未知年份写成字符串 "0"，直接透出去卡片下面会顶着「0」
        year = str(it.get("vod_year") or "").strip()
        if year in ("0", "0000", "None", "null"):
            year = ""
        remarks = str(it.get("vod_remarks") or "").strip()
        if not remarks:
            remarks = year
        if not remarks and str(it.get("type_id") or "") == "20":
            remarks = "短剧"
        return {
            "vod_id": vid,
            "vod_name": name,
            "vod_pic": pic,
            "vod_remarks": remarks,
            "vod_year": year,
            "vod_area": str(it.get("vod_area") or ""),
            "vod_class": str(it.get("vod_class") or ""),
            "vod_actor": str(it.get("vod_actor") or ""),
            "vod_director": str(it.get("vod_director") or ""),
            "vod_content": _clean(it.get("vod_blurb") or it.get("vod_content") or ""),
            "type_id": str(it.get("type_id") or tid_hint or ""),
        }

    def _list(self, tid, pg, kw="*"):
        pg = int(pg) if str(pg).isdigit() else 1
        qs = "wd=" + quote(str(kw or "*")) + "&page=" + str(pg)
        if tid:
            qs += "&type=" + quote(str(tid))
        d = (self._api("/api/v1/search", qs) or {}).get("data") or {}
        out = []
        for it in (d.get("hits") or []):
            v = self._vod(it, tid)
            if v:
                out.append(v)
        return {"list": out,
                "total": int(d.get("total") or 0),
                "pages": int(d.get("total_pages") or 1)}

    # ---------------- 首页 ----------------
    def homeContent(self, filter=False):
        result = {"class": [{"type_id": str(t), "type_name": n} for t, n in CATS],
                  "filters": {}, "list": []}
        vods, seen = [], set()
        for tid in (2, 1, 4):
            try:
                r = self._list(tid, 1, "")
            except Exception as e:
                print("[sz] home", tid, e)
                continue
            for v in r["list"]:
                if v["vod_id"] in seen:
                    continue
                seen.add(v["vod_id"])
                vods.append(v)
        result["list"] = vods
        return result

    def homeVideoContent(self):
        return {"list": self.homeContent(False).get("list") or []}

    # ---------------- 分类 ----------------
    def categoryContent(self, tid, pg, filter, extend):
        pg = int(pg) if str(pg).isdigit() else 1
        result = {"list": [], "page": pg, "pagecount": 1, "limit": 20, "total": 0}
        try:
            r = self._list(str(tid or ""), pg, "*")
        except Exception as e:
            print("[sz] category", tid, pg, e)
            return result
        result["list"] = r["list"]
        result["pagecount"] = max(1, int(r["pages"]))
        result["total"] = int(r["total"])
        result["limit"] = max(1, len(r["list"]))
        return result

    # ---------------- 搜索 ----------------
    def searchContent(self, key, quick, pg="1"):
        pg = int(pg) if str(pg).isdigit() else 1
        try:
            r = self._list("", pg, str(key or ""))
        except Exception as e:
            print("[sz] search", e)
            return {"list": [], "page": pg, "pagecount": 1, "total": 0}
        return {"list": r["list"], "page": pg,
                "pagecount": max(1, int(r["pages"])), "total": int(r["total"])}

    # ---------------- 详情 ----------------
    @staticmethod
    def _line_rank(name):
        """线路排序：小芒4K 置顶 → 4K/2160 → 蓝光 → 1080/超清 → 720/高清 → 其余保持站点原序

        小芒4K 是用户 2026-11-09 点名要排第一条的线路，优先于所有带 4K 的档位。
        """
        n = str(name or "").lower()
        if "小芒" in n:
            return 5
        if "4k" in n or "2160" in n:
            return 10
        if "蓝光" in n or "blu" in n:
            return 20
        if "1080" in n or "超清" in n or re.search(r"(?<![a-z])hd(?![a-z])", n):
            return 30
        if "720" in n or "高清" in n:
            return 40
        return 60

    def _token(self, vid, code, name, idx, tk):
        """播放令牌：base64url(JSON)，避开 $ # | 被壳误切"""
        return _b64u_enc(json.dumps(
            {"v": str(vid), "c": str(code), "n": str(name), "i": int(idx), "t": str(tk)},
            ensure_ascii=False).encode("utf-8"))

    def apiDetail(self, vid):
        """详情：只发一个请求，站点给几条线路就返回几条，不做任何体检/隐藏"""
        r = self._api("/api/v1/video/detail", "id=" + quote(str(vid)))
        vi = (r.get("data") or {}).get("vod_info") or {}
        real_id = str(vi.get("vod_id") or vid)
        lines = []
        for i, ln in enumerate(vi.get("vod_url_with_player") or []):
            name = str(ln.get("name") or ln.get("code") or ("线路%d" % (i + 1)))
            code = str(ln.get("code") or "")
            segs = [x for x in str(ln.get("url") or "").split("#") if x]
            eps = []
            for j, seg in enumerate(segs):
                d = seg.find("$")
                ep_name, tk = (seg[:d], seg[d + 1:]) if d >= 0 else (seg, "")
                if not ep_name:
                    ep_name = "第%d集" % (j + 1)
                eps.append({
                    "name": ep_name,
                    "tk": tk,
                    "idx": j,
                    "tok": self._token(real_id, code, name, j, tk),
                })
            # 没有分集的线路也保留（用户要求不隐藏），只是点不了
            lines.append({"name": name, "code": code, "eps": eps})
        order = sorted(range(len(lines)), key=lambda k: (self._line_rank(lines[k]["name"]), k))
        lines = [lines[k] for k in order]
        pic = _pic(vi.get("vod_pic") or "")
        dyear = str(vi.get("vod_year") or "").strip()
        if dyear in ("0", "0000", "None", "null"):
            dyear = ""
        return {
            "id": real_id,
            "name": str(vi.get("vod_name") or ""),
            "pic": pic,
            "remarks": str(vi.get("vod_remarks") or vi.get("vod_state") or ""),
            "year": dyear,
            "area": str(vi.get("vod_area") or ""),
            "cls": str(vi.get("vod_class") or ""),
            "actor": str(vi.get("vod_actor") or ""),
            "director": str(vi.get("vod_director") or ""),
            "content": _clean(vi.get("vod_content") or vi.get("vod_blurb") or ""),
            "type": str(vi.get("type_id") or ""),
            "lines": lines,
        }

    def detailContent(self, ids):
        vid = ids[0] if isinstance(ids, (list, tuple)) else ids
        vid = str(vid or "")
        if not vid:
            return {"list": []}
        try:
            d = self.apiDetail(vid)
        except Exception as e:
            print("[sz] detail", e)
            return {"list": []}
        try:
            self._detail_cache[vid] = d
            if len(self._detail_cache) > 60:
                self._detail_cache.clear()
        except Exception:
            pass
        froms, urls = [], []
        for ln in d["lines"]:
            cells = ["%s$%s" % (ep["name"], ep["tok"]) for ep in ln["eps"]]
            # 空线路占位：保留线路名，点进去给出明确失败原因
            froms.append(ln["name"])
            urls.append("#".join(cells) if cells else "暂无可用剧集$")
        vod = {
            "vod_id": d["id"],
            "vod_name": d["name"] or ("影片 " + vid),
            "vod_pic": d["pic"],
            "vod_remarks": d["remarks"],
            "vod_year": d["year"],
            "vod_area": d["area"],
            "vod_actor": d["actor"],
            "vod_director": d["director"],
            "vod_content": d["content"],
            "type_name": d["cls"],
            "vod_play_from": "$$$".join(froms),
            "vod_play_url": "$$$".join(urls),
        }
        print("[sz] 详情 %s：%d 条线路（%s）" % (
            vod["vod_name"], len(froms),
            " / ".join("%s×%d" % (l["name"], len(l["eps"])) for l in d["lines"])))
        return {"list": [vod]}

    # ---------------- 换票 + 解析 ----------------
    def _resolve(self, tk, timeout=10):
        """票根 → 直链。返回 (url, why)"""
        s0 = str(tk or "")
        if s0.startswith("DIRECT:"):
            return s0[7:], ""
        if not s0:
            return "", "该线路没有剧集"
        r = self._post("/api/v1/vod/ticket", {"token": s0}, timeout=timeout)
        if not r or r.get("code") != 1:
            return "", str((r or {}).get("msg") or "换票失败")
        ticket = str((r.get("data") or {}).get("ticket") or "")
        if not ticket:
            return "", "换票未返回票据"
        try:
            o = json.loads(_b64u_dec(ticket.split(".")[0]).decode("utf-8", "ignore"))
        except Exception:
            return "", "票据解析失败"
        dd = o.get("data") or {}
        api = str(dd.get("parse_api") or "").replace("\\u00026", "&")
        du = str(dd.get("url") or "")
        if not api or not du:
            return "", "票据信息缺失"
        if api.startswith("/"):
            target = BASE + api + quote(du, safe="")
        elif "{md5}" in api:
            target = api.replace("{md5}", du)
        else:
            target = api + quote(du, safe="")
        try:
            rp = self.s.get(target, headers=self._headers(), timeout=timeout)
        except Exception as e:
            return "", self._errmsg(e)
        txt = rp.text or ""
        j = None
        try:
            j = json.loads(txt[txt.find("{"):])
        except Exception:
            j = None
        if isinstance(j, dict):
            u = str(j.get("url") or (j.get("data") or {}).get("url") or "")
            if u:
                return u, ""
            return "", str(j.get("msg") or j.get("system") or "解析未返回地址")[:60]
        if re.match(r"^\s*https?://", txt):
            return txt.strip(), ""
        return "", "解析返回异常"

    # ---------------- 播放地址体检 ----------------
    def _fourcc(self, url, timeout=6):
        """按顺序找第一个视频类四字符码（音轨 mp4a 排在视轨前面时不能误判）"""
        try:
            r = self.s.get(url, headers=({"User-Agent": None, "Referer": None,
                                          "Range": "bytes=0-1048575"} if _nohdr(url) else
                                         {"User-Agent": UA, "Referer": REF,
                                          "Accept-Encoding": "identity",
                                          "Range": "bytes=0-1048575"}), timeout=timeout)
            b = r.content or b""
        except Exception:
            return b""
        pos = 0
        while True:
            i = b.find(b"stsd", pos)
            if i < 0 or i + 12 > len(b):
                return b""
            code = b[i + 8:i + 12]
            if code == b"mp4a":
                pos = i + 4
                continue
            if code in VIDEO_FOURCC or code in (b"bvc2", b"bv2C", b"av01", b"vp09", b"encv"):
                return code
            pos = i + 4

    def _check(self, url, timeout=6):
        """返回 (ok, kind, why)。四字符码须在 hvc1/hev1/avc1/avc3、须有视频轨、中段须回 206"""
        u = str(url or "")
        if not re.match(r"^https?://", u, re.I):
            return False, "", "地址无效"
        h = ({"User-Agent": None, "Referer": None, "Range": "bytes=0-4095"} if _nohdr(u) else
             {"User-Agent": UA, "Referer": REF, "Accept-Encoding": "identity",
              "Range": "bytes=0-4095"})
        try:
            r = self.s.get(u, headers=h, timeout=timeout, allow_redirects=True)
        except Exception as e:
            return False, "", self._errmsg(e)
        if r.status_code >= 400:
            return False, "", "HTTP %d" % r.status_code
        b = r.content or b""
        if not b:
            return False, "", "空响应"
        txt = b.decode("utf-8", "ignore")
        if "#EXTM3U" in txt:
            segs = [x.strip() for x in txt.splitlines()
                    if x.strip() and not x.strip().startswith("#")]
            if not segs:
                return False, "hls", "空播放列表"
            su = urljoin(r.url or u, segs[0])
            if re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", su, re.I):
                return False, "hls", "广告占位源"
            try:
                rs = self.s.get(su, headers=({"User-Agent": None, "Referer": None,
                                              "Range": "bytes=0-2047"} if _nohdr(su) else
                                             {"User-Agent": UA, "Referer": REF,
                                              "Range": "bytes=0-2047"}), timeout=timeout)
                if rs.status_code >= 400 or not (rs.content or b""):
                    return False, "hls", "分片取不到"
            except Exception:
                return False, "hls", "分片超时"
            return True, "hls", ""
        is_mp4 = b[4:8] in (b"ftyp", b"styp")
        is_ts = b[0] == 0x47 and len(b) > 188 and b[188] == 0x47
        is_flv = b[:3] == b"FLV"
        if not (is_mp4 or is_ts or is_flv):
            return False, "", "非视频容器"
        if is_mp4:
            code = self._fourcc(u, timeout)
            if code and code not in VIDEO_FOURCC:
                return False, "mp4", "四字符码不可播(%s)" % code.decode("latin1", "ignore")
            cl = 0
            try:
                cl = int(r.headers.get("Content-Length") or 0)
            except Exception:
                cl = 0
            off = 1000000 if (cl <= 0 or cl > 1200000) else max(0, cl // 2)
            try:
                rm = self.s.get(u, headers=({"User-Agent": None, "Referer": None,
                                             "Range": "bytes=%d-%d" % (off, off + 2047)} if _nohdr(u) else
                                            {"User-Agent": UA, "Referer": REF,
                                             "Range": "bytes=%d-%d" % (off, off + 2047)}),
                                timeout=timeout)
                if rm.status_code != 206:
                    return False, "mp4", "中段不支持 Range(%d)" % rm.status_code
            except Exception:
                return False, "mp4", "中段请求失败"
            return True, "mp4", ""
        return True, "ts", ""

    # ---------------- 播放 ----------------
    def _alt_lines(self, vid, idx, cur):
        """同集其它线路兜底：最多试 3 条，总预算 20 秒（壳的详情预算是 40 秒）"""
        d = self._detail_cache.get(vid)
        if not d:
            try:
                d = self.apiDetail(vid)
                self._detail_cache[vid] = d
            except Exception:
                return "", "", ""
        t0 = time.time()
        tried = 0
        for ln in d["lines"]:
            if ln["name"] == cur:
                continue
            if tried >= 3 or (time.time() - t0) > 20:
                break
            eps = ln["eps"]
            ep = eps[idx] if idx < len(eps) else (eps[0] if len(eps) == 1 else None)
            if not ep:
                continue
            tried += 1
            try:
                u, _why = self._resolve(ep["tk"])
            except Exception:
                continue
            if not u:
                continue
            ok, kind, _why2 = self._check(u, 6)
            if ok:
                return u, kind, ln["name"]
        return "", "", ""

    def playerContent(self, flag, id, vipFlags=None):
        out = {"parse": 0, "playUrl": "", "url": "", "header": {}}
        tok = str(id or "")
        vid, idx, tk, code = "", 0, tok, ""
        try:
            pd = json.loads(_b64u_dec(tok).decode("utf-8", "ignore"))
            vid = str(pd.get("v") or "")
            idx = int(pd.get("i") or 0)
            tk = str(pd.get("t") or "")
            code = str(pd.get("c") or "")
        except Exception:
            # 兼容不带 JSON 的裸令牌
            vid, tk = "", tok
        if not tk:
            print("[sz] 播放失败：该线路没有剧集")
            return out
        try:
            url, why = self._resolve(tk)
        except Exception as e:
            url, why = "", self._errmsg(e)
        if url:
            ok, kind, why2 = self._check(url, 6)
            if ok:
                out["url"] = url
                # 芒果这类 CDN 带 UA/Referer 反而 403，播放器也用裸请求
                out["header"] = {} if _nohdr(url) else {"User-Agent": UA, "Referer": REF}
                return out
            why = why2 or why
        # 换线兜底：线路名原样保留，只把「换到了哪条」写进日志
        if vid:
            u2, _k2, name2 = self._alt_lines(vid, idx, str(flag or ""))
            if u2:
                print("[sz] 「%s」播不了（%s），已切到「%s」" % (flag or code, why, name2))
                out["url"] = u2
                out["header"] = {} if _nohdr(u2) else {"User-Agent": UA, "Referer": REF}
                return out
        hint = KNOWN_BAD.get(code, "")
        print("[sz] 播放失败：%s%s" % (why or "未取到播放地址",
                                     ("（站点侧已知：%s）" % hint) if hint else ""))
        out["url"] = ""
        return out
