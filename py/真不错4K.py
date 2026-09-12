#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# TVBox Python 爬虫 · 星海影视 zbc4k (xhappcmszbc.zbc4k.app)
# ------------------------------------------------------------
# 协议（2026-08-29 全逆向）:
#   签名:  X-Sign = HMAC-SHA256(pk, "GET|/api/v1{path}|{ts}|{nonce}|{query}")
#   query: _ep={AES空体}&_ts={ms}&业务参数&platform=app  ← 全部按字母序!
#   _ep:   base64url(IV + AES-256-CBC(sha256_hex(pk+win)[:32], json))
#   响应:  data = base64(IV + AES-256-CBC(同key, json)), win = resp.ts//60000
#   POST:  body=明文JSON + X-Body-Hash=sha256(body), msg 末段=bodyHash
#   时钟:  服务器快 ~20s → ts 加 25s 补偿
# 依赖: 无 (纯 urllib + hashlib + hmac)
# ============================================================
import sys
try:
    from base.spider import Spider as _BaseSpider   # TVBox py_spider
except Exception:
    _BaseSpider = object

import json
import time
import hmac
import base64
import hashlib
import secrets
import urllib.request
import urllib.parse
import urllib.error

HOST = "https://xhappcmszbc.zbc4k.app/api/v1"
PK = "faa2d8da28ebd29c"
UA = "okhttp/4.12.0"
APP_HASH = "2f570fc1e7996742196c188ed30895334675bcee2901b7d7fc63e5467aff9f11"
# 固定设备指纹（服务器仅做统计，无绑定校验）
FP = hashlib.sha256(b"zbc4k-tvbox-spider").hexdigest()
CLOCK_SKEW = 25          # 服务器时钟超前补偿(秒)
PAGE_SIZE = 20           # 服务器默认每页

# 直连可播源优先级（网页源排后, NBY token 源丢弃）
WEB_PREFIX = ("http://v.youku", "http://v.qq", "https://v.youku",
              "https://v.qq", "https://www.youku", "https://www.iqiyi",
              "https://www.mgtv", "https://www.bilibili")

# 分类兜底（正常从 /init types 动态拉取）
FALLBACK_TYPES = [
    (1, "电视剧"), (2, "动漫"), (3, "电影"), (4, "综艺"), (5, "短剧"), (7, "纪录"),
]


# 直播 logo 公共镜像（服务器不下发 logo; 加载失败仅无图标, 不影响播放）
LOGO_TPL = "https://live.fanmingming.com/tv/{name}.png"


class Spider(_BaseSpider):

    # ---------------- 协议层 ----------------
    def _win_key(self, win):
        return hashlib.sha256((PK + str(win)).encode()).hexdigest().encode()[:32]

    def _b64url(self, raw):
        return base64.b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")

    def _make_ep(self, ms):
        """空 JSON 加密体（服务器不校验 _ep 内容, 业务参数走明文 query）"""
        from Crypto.Cipher import AES  # noqa: 仅签名注释用
        return None

    def _ep(self):
        """纯 Python 实现 AES-256-CBC（无 pycryptodome 依赖, 手写 AES）"""
        ms = int(time.time() * 1000)
        win = ms // 60000
        key = self._win_key(win)
        iv = secrets.token_bytes(16)
        # AES 软实现
        ct = self._aes_cbc_encrypt(key, iv, self._pkcs7(b"{}"))
        return self._b64url(iv + ct), ms

    # ---- 纯 Python AES（CBC/解密也要用; 若环境有 pycryptodome 则优先） ----
    def _try_import_crypto(self):
        try:
            from Crypto.Cipher import AES as _AES
            return _AES
        except Exception:
            return None

    def _aes_cbc_encrypt(self, key, iv, data):
        AES = self._try_import_crypto()
        if AES is not None:
            return AES.new(key, AES.MODE_CBC, iv=iv).encrypt(data)
        return self._pure_aes_cbc(key, iv, data, encrypt=True)

    def _aes_cbc_decrypt(self, key, iv, data):
        AES = self._try_import_crypto()
        if AES is not None:
            return AES.new(key, AES.MODE_CBC, iv=iv).decrypt(data)
        return self._pure_aes_cbc(key, iv, data, encrypt=False)

    # 极简纯 Python AES-256 实现（S盒/逆盒/密钥扩展/ CBC 模式）
    _SBOX = None
    _INV_SBOX = None

    @classmethod
    def _init_sbox(cls):
        if cls._SBOX is not None:
            return
        p = q = 1
        sbox = [0] * 256
        while True:
            p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
            q ^= q << 1
            q ^= q << 2
            q ^= q << 4
            q &= 0xFF
            if q & 0x80:
                q ^= 0x09
            xformed = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
            sbox[p] = (xformed ^ 0x63) & 0xFF
            if p == 1:
                break
        sbox[0] = 0x63
        inv = [0] * 256
        for i, v in enumerate(sbox):
            inv[v] = i
        cls._SBOX = sbox
        cls._INV_SBOX = inv
        # RCON
        cls._RCON = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]

    def _xtime(self, a):
        a <<= 1
        if a & 0x100:
            a ^= 0x11B
        return a & 0xFF

    def _mul(self, a, b):
        r = 0
        for _ in range(8):
            if b & 1:
                r ^= a
            b >>= 1
            a = self._xtime(a)
        return r

    def _key_expand(self, key):
        self._init_sbox()
        nk = 8                      # AES-256
        rounds = 14
        words = [list(key[i * 4:(i + 1) * 4]) for i in range(nk)]
        for i in range(nk, 4 * (rounds + 1)):
            t = list(words[i - 1])
            if i % nk == 0:
                t = t[1:] + t[:1]
                t = [self._SBOX[b] for b in t]
                t[0] ^= self._RCON[i // nk]
            elif i % nk == 4:
                t = [self._SBOX[b] for b in t]
            words.append([words[i - nk][j] ^ t[j] for j in range(4)])
        return words

    def _aes_block(self, block, w, encrypt=True):
        self._init_sbox()
        s = self._SBOX
        inv = self._INV_SBOX
        rounds = 14
        st = [[block[r + 4 * c] for c in range(4)] for r in range(4)]  # state[r][c]

        def addrk(rnd):
            for c in range(4):
                for r in range(4):
                    st[r][c] ^= w[rnd * 4 + c][r]

        addrk(0)
        for rnd in range(1, rounds + 1):
            last = (rnd == rounds)
            # SubBytes
            for r in range(4):
                for c in range(4):
                    st[r][c] = s[st[r][c]] if encrypt else inv[st[r][c]]
            if not last:
                # ShiftRows
                if encrypt:
                    for r in range(1, 4):
                        st[r] = st[r][r:] + st[r][:r]
                else:
                    for r in range(1, 4):
                        st[r] = st[r][-r:] + st[r][:-r]
                # MixColumns
                for c in range(4):
                    a = [st[r][c] for r in range(4)]
                    if encrypt:
                        st[0][c] = self._mul(a[0], 2) ^ self._mul(a[1], 3) ^ a[2] ^ a[3]
                        st[1][c] = a[0] ^ self._mul(a[1], 2) ^ self._mul(a[2], 3) ^ a[3]
                        st[2][c] = a[0] ^ a[1] ^ self._mul(a[2], 2) ^ self._mul(a[3], 3)
                        st[3][c] = self._mul(a[0], 3) ^ a[1] ^ a[2] ^ self._mul(a[3], 2)
                    else:
                        st[0][c] = self._mul(a[0], 14) ^ self._mul(a[1], 11) ^ self._mul(a[2], 13) ^ self._mul(a[3], 9)
                        st[1][c] = self._mul(a[0], 9) ^ self._mul(a[1], 14) ^ self._mul(a[2], 11) ^ self._mul(a[3], 13)
                        st[2][c] = self._mul(a[0], 13) ^ self._mul(a[1], 9) ^ self._mul(a[2], 14) ^ self._mul(a[3], 11)
                        st[3][c] = self._mul(a[0], 11) ^ self._mul(a[1], 13) ^ self._mul(a[2], 9) ^ self._mul(a[3], 14)
            addrk(rnd)
        out = bytearray(16)
        for c in range(4):
            for r in range(4):
                out[r + 4 * c] = st[r][c]
        return bytes(out)

    def _pure_aes_cbc(self, key, iv, data, encrypt):
        w = self._key_expand(key)
        if encrypt:
            out = b""
            prev = iv
            for i in range(0, len(data), 16):
                blk = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
                enc = self._aes_block(blk, w, True)
                out += enc
                prev = enc
            return out
        out = b""
        prev = iv
        for i in range(0, len(data), 16):
            blk = data[i:i + 16]
            dec = self._aes_block(blk, w, False)
            out += bytes(a ^ b for a, b in zip(dec, prev))
            prev = blk
        return out

    def _pkcs7(self, data):
        n = 16 - (len(data) % 16)
        return data + bytes([n]) * n

    def _unpkcs7(self, data):
        if not data:
            return data
        n = data[-1]
        if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
            return data[:-n]
        return data

    # ---------------- HTTP 层 ----------------
    def _decrypt(self, resp):
        """响应解密: data = b64(IV + AES(key, json)), key 按响应 ts 窗口"""
        try:
            raw = base64.b64decode(resp.get("data", ""))
            ts = resp.get("ts", 0)
            wins = [ts // 60000] if ts else []
            wins += [0]
            for w in wins:
                try:
                    key = self._win_key(w)
                    pt = self._aes_cbc_decrypt(key, raw[:16], raw[16:])
                    txt = self._unpkcs7(pt).decode("utf-8")
                    if txt.startswith("{"):
                        return json.loads(txt)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _headers(self):
        return {
            "User-Agent": UA,
            "X-App-Pkg": "com.tbeoil.axy",
            "X-App-Ver": "5.1.1",
            "X-Device-FP": FP,
            "X-App-Hash": APP_HASH,
        }

    def _request(self, path, params=None, method="GET", body=None, retries=2):
        """带重试的签名请求, 返回解密后的 dict
        注意: query 值含特殊字符(| : / = 等)必须 percent-encode, 否则 403
        """
        for _ in range(retries + 1):
            ep, ms = self._ep()
            qd = {"_ep": ep, "_ts": str(ms), "platform": "app"}
            for k, v in (params or {}).items():
                qd[k] = urllib.parse.quote(str(v), safe="")
            # ★ 字母序排列（协议硬性要求）
            q = "&".join(f"{k}={qd[k]}" for k in sorted(qd))
            ts = str(int(time.time()) + CLOCK_SKEW)
            nonce = secrets.token_hex(8)
            if method == "GET":
                msg = f"GET|/api/v1{path}|{ts}|{nonce}|{q}"
            else:
                bh = hashlib.sha256(body or b"").hexdigest()
                msg = f"POST|/api/v1{path}|{ts}|{nonce}|{q}|{bh}"
            sign = hmac.new(PK.encode(), msg.encode(), hashlib.sha256).hexdigest()
            hdrs = self._headers()
            hdrs.update({"X-Timestamp": ts, "X-Nonce": nonce, "X-Sign": sign})
            url = f"{HOST}{path}?{q}"
            data = None
            if method == "POST":
                hdrs["Content-Type"] = "application/json"
                hdrs["X-Body-Hash"] = hashlib.sha256(body or b"").hexdigest()
                data = body
            req = urllib.request.Request(url, data=data, headers=hdrs)
            try:
                r = urllib.request.urlopen(req, timeout=20)
                resp = json.loads(r.read())
                dec = self._decrypt(resp)
                if dec is not None:
                    return dec
                # 明文响应兜底
                if isinstance(resp, dict) and "code" in resp and "data" in resp:
                    return resp
            except urllib.error.HTTPError as e:
                try:
                    resp = json.loads(e.read())
                    dec = self._decrypt(resp)
                    if dec is not None:
                        return dec
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(0.6)
        return None

    def _get(self, path, params=None):
        return self._request(path, params, "GET")

    def _post(self, path, payload):
        return self._request(path, {}, "POST",
                             json.dumps(payload, separators=(",", ":")).encode())

    # ---------------- TVBox 接口 ----------------
    def init(self, cfg):
        self._types = None
        self._live_cats = None      # [(id, name)]
        self._live_channels = None  # 全量频道缓存
        self._line_codes = {}       # 线路显示名 → player_code（点播解析用）
        return ""

    # ---------------- 直播（站点内动态分类） ----------------
    def _load_live(self):
        """拉取并缓存直播分类+频道（TVBox 多次翻页/详情复用）"""
        if self._live_channels is not None:
            return
        d = self._get("/live/channels", {})
        if not d or not isinstance(d.get("data"), dict):
            self._live_cats, self._live_channels = [], []
            return
        data = d["data"]
        cats = [(c["id"], c["name"]) for c in data.get("categories", [])
                if c.get("status", 1) == 1]
        cats.sort(key=lambda x: x[0])
        self._live_cats = cats
        self._live_channels = data.get("channels", [])

    def _live_ch_by_id(self, chid):
        for ch in (self._live_channels or []):
            if ch.get("id") == chid:
                return ch
        return None

    def _load_types(self):
        if self._types is not None:
            return self._types
        d = self._get("/init")   # /init 免签; 但统一走签名亦可
        types = None
        if d and isinstance(d.get("data"), dict):
            types = d["data"].get("types")
        if not types:
            # 免签兜底（不带签名头）
            try:
                req = urllib.request.Request(
                    HOST + "/init?platform=app", headers=self._headers())
                resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
                dec = self._decrypt(resp)
                if dec and isinstance(dec.get("data"), dict):
                    types = dec["data"].get("types")
            except Exception:
                pass
        if types:
            self._types = [(t["type_id"], t["type_name"]) for t in types
                           if t.get("type_status", 1) == 1]
        else:
            self._types = FALLBACK_TYPES
        return self._types

    def homeContent(self, filter):
        classes = []
        for tid, name in self._load_types():
            classes.append({"type_id": str(tid), "type_name": name})
        # 直播合并为单项分类（筛选切换分组）
        filters = {}
        try:
            self._load_live()
            if self._live_cats:
                classes.append({"type_id": "live", "type_name": "直播"})
                vals = [{"n": "全部", "v": ""}]
                for cid, cname in self._live_cats:
                    vals.append({"n": cname, "v": str(cid)})
                filters["live"] = [{"key": "group", "name": "分组",
                                    "value": vals}]
        except Exception:
            pass
        return {"class": classes, "filters": filters}

    def homeVideoContent(self):
        # 不出首页推荐（用户要求去掉"精选"板块）
        return {}

    def categoryContent(self, tid, pg, filter1, ext):
        # ---- 直播（单项分类 + 分组筛选） ----
        if str(tid) == "live":
            return self._live_category(int(pg or 1), ext or {})
        pg = int(pg or 1)
        d = self._get("/category/videos", {"type_id": tid, "page": pg})
        videos = []
        total = pg + 1
        if d and isinstance(d.get("data"), dict):
            data = d["data"]
            lst = data.get("list") or data.get("videos") or []
            for v in lst:
                videos.append(self._card(v))
            # ★ 服务器标准分页字段: total=总数, page_size=每页
            try:
                t = int(data.get("total") or 0)
                ps = int(data.get("page_size") or PAGE_SIZE)
                if t > 0 and ps > 0:
                    total = (t + ps - 1) // ps
            except Exception:
                total = pg + (1 if len(videos) >= PAGE_SIZE else 0)
        if not videos:
            return {"list": [], "limit": PAGE_SIZE, "total": pg}
        return {"list": videos, "limit": PAGE_SIZE, "total": max(total, pg)}

    def _live_category(self, pg, ext):
        """直播列表（合并单项分类, ext.group 按分组筛选, 50/页）"""
        self._load_live()
        group = str(ext.get("group") or "")
        chans = [ch for ch in (self._live_channels or [])
                 if not group or str(ch.get("category_id")) == group]
        LIVE_PAGE = 50
        total = max((len(chans) + LIVE_PAGE - 1) // LIVE_PAGE, 1)
        start = (pg - 1) * LIVE_PAGE
        videos = []
        for ch in chans[start:start + LIVE_PAGE]:
            videos.append({
                "vod_id": f"live_{ch['id']}",
                "vod_name": ch.get("name", ""),
                "vod_pic": self._live_logo(ch.get("name", "")),
                "vod_remarks": "直播",
            })
        return {"list": videos, "limit": LIVE_PAGE, "total": total}

    @staticmethod
    def _live_logo(name):
        """频道 logo: 公共镜像按名匹配（CCTV1综合→CCTV1; 失败仅无图标）"""
        import re
        if not name:
            return ""
        m = re.match(r"^(CCTV[\d]+[+\-]?)", name.upper())
        key = m.group(1) if m else name
        return LOGO_TPL.format(name=urllib.parse.quote(key))

    def _live_detail(self, chid):
        """直播频道: sources[] 每个源一条线路"""
        self._load_live()
        ch = self._live_ch_by_id(chid)
        if not ch:
            return {}
        srcs = ch.get("sources") or []
        froms, urls, seen = [], [], {}
        for i, src in enumerate(srcs):
            name = src.get("name") or f"线路{i + 1}"
            # 同名线路加序号区分（TVBox 显示）
            if froms.count(name):
                seen[name] = seen.get(name, 1) + 1
                name = f"{name}{seen[name]}"
            url = src.get("url", "")
            if not url:
                continue
            froms.append(name)
            urls.append(f"直播${url}")
        vod = {
            "vod_id": f"live_{chid}",
            "vod_name": ch.get("name", ""),
            "vod_pic": self._live_logo(ch.get("name", "")),
            "vod_remarks": "直播中",
            "vod_area": "直播",
            "vod_content": "电视直播频道，多线路可选。",
            "vod_play_from": "$$$".join(froms),
            "vod_play_url": "$$$".join(urls),
        }
        return {"list": [vod]}

    def _card(self, v):
        pic = v.get("vod_pic", "")
        # http→https 统一升级（部分 TVBox 内核对明文图有 mixed-content 限制; 已实测各域名均支持 https）
        if pic.startswith("http://"):
            pic = "https://" + pic[7:]
        return {
            "vod_id": str(v.get("vod_id", "")),
            "vod_name": v.get("vod_name", ""),
            "vod_pic": pic,
            "vod_remarks": v.get("vod_remarks", ""),
        }

    def detailContent(self, ids):
        vid = ids[0]
        # ---- 直播频道详情（多源=多线路） ----
        if str(vid).startswith("live_"):
            return self._live_detail(int(str(vid)[5:]))
        d = self._get(f"/vod/detail/{vid}")
        if not d or not isinstance(d.get("data"), dict):
            return {}
        info = d["data"].get("info") or {}
        # 播放源（POST）
        p = self._post("/vod/play-info", {"vod_id": int(vid)})
        sources, eps_map = [], {}
        line_codes = {}
        if p and isinstance(p.get("data"), dict):
            data = p["data"]
            pm = data.get("playermap") or {}
            # ★ 与 APP 一致: 显示 player_name, 按 player_sorting 升序（稳定排序）
            items = []
            for src in data.get("play_sources", []):
                frm = src.get("from", "")
                eps = src.get("episodes", [])
                if not frm or not eps:
                    continue
                m = pm.get(frm) or {}
                disp = m.get("player_name") or frm
                try:
                    sorting = int(m.get("player_sorting"))
                except Exception:
                    sorting = 999
                items.append((sorting, disp, frm, eps))
            items.sort(key=lambda x: x[0])
            for sorting, disp, frm, eps in items:
                if disp in eps_map:      # 同名线路加序号
                    k = 2
                    while f"{disp}{k}" in eps_map:
                        k += 1
                    disp = f"{disp}{k}"
                sources.append(disp)
                line_codes[disp] = frm
                eps_map[disp] = [f"{e.get('name', '')}${e.get('url', '')}"
                                 for e in eps]
        # 缓存线路名→代码映射（playerContent 按 flag 取）
        self._line_codes = line_codes
        vod = {
            "vod_id": str(vid),
            "vod_name": info.get("vod_name", ""),
            "vod_pic": self._upgrade_pic(info.get("vod_pic", "")),
            "type_name": "",
            "vod_year": info.get("vod_year", ""),
            "vod_area": info.get("vod_area", ""),
            "vod_actor": info.get("vod_actor", ""),
            "vod_director": info.get("vod_director", ""),
            "vod_remarks": info.get("vod_remarks", ""),
            "vod_content": self._strip_html(info.get("vod_content", "")),
            "vod_play_from": "$$$".join(sources),
            "vod_play_url": "",
        }
        for tid, _ in self._load_types():
            if str(tid) == str(info.get("type_id")):
                # type_name 兜底
                pass
        play_urls = []
        for disp in sources:
            play_urls.append("#".join(eps_map[disp]))   # 已是 "name$code|url" 格式
        vod["vod_play_url"] = "$$$".join(play_urls)
        return {"list": [vod]}

    @staticmethod
    def _upgrade_pic(pic):
        if pic.startswith("http://"):
            return "https://" + pic[7:]
        return pic

    @staticmethod
    def _strip_html(s):
        import re
        return re.sub(r"<[^>]+>", "", s or "").strip()

    def searchContent(self, key, quick, pg="1"):
        pg = int(pg or 1)
        # key 传原文, _request 内统一编码
        d = self._get("/search", {"keyword": key, "page": pg})
        videos = []
        total = pg
        if d and isinstance(d.get("data"), dict):
            data = d["data"]
            for v in data.get("list", []):
                videos.append(self._card(v))
            try:
                t = int(data.get("total") or 0)
                ps = int(data.get("page_size") or PAGE_SIZE)
                if t > 0 and ps > 0:
                    total = (t + ps - 1) // ps
            except Exception:
                total = pg + (1 if len(videos) >= PAGE_SIZE else 0)
        return {"list": videos, "limit": PAGE_SIZE, "total": total}

    def searchContentPage(self, key, quick, pg):
        return self.searchContent(key, quick, pg)

    def playerContent(self, flag, id, vipFlags):
        url = id
        header = {"User-Agent": UA}
        # 线路代码: 从 detailContent 缓存的映射取（flag=线路显示名）
        code = self._line_codes.get(flag) or flag
        # ★ token 源（JD-/NBY-/任意非http前缀）和网页源统一走 parse
        need_parse = (not url.startswith(("http://", "https://"))) or \
                     url.startswith(WEB_PREFIX)
        if need_parse:
            # /player/parse 解析（重试一次）
            for _ in range(2):
                r = self._get("/player/parse", {"url": url, "from": code})
                if r and isinstance(r.get("data"), dict):
                    inner = r["data"].get("data") or {}
                    real = inner.get("url")
                    if real:
                        # 兼容两种头字段名（qq4k→User-Agent, NBY→UA）
                        play_ua = inner.get("User-Agent") or inner.get("UA")
                        return {"parse": 0, "url": real,
                                "header": {"User-Agent": play_ua} if play_ua else header}
                time.sleep(0.8)
            # 解析失败兜底: 交 TVBox 自带解析（webview）
            if url.startswith("http"):
                return {"parse": 1, "url": url, "header": header}
            return {"parse": 0, "url": url, "header": header}
        # 直连 m3u8/mp4（直播/直连源）
        return {"parse": 0, "url": url, "header": header}

    def localProxy(self, params):
        return None

    def isVideoFormat(self, url):
        return ".m3u8" in url or ".mp4" in url

    def manualVideoResolve(self, url):
        return url


# ============================================================
# 直播源生成（独立运行: python3 zbc4k_spider.py live）
# 生成 TVBox 直播配置 live.txt（分组+频道）
# ============================================================
def gen_live(path="live.txt"):
    s = Spider()
    print("拉取直播频道 ...")
    d = s._get("/live/channels", {})
    if not d or not isinstance(d.get("data"), dict):
        print("!! 频道拉取失败")
        return
    data = d["data"]
    cats = {c["id"]: c["name"] for c in data.get("categories", [])}
    chans = data.get("channels", [])
    groups = {}
    for ch in chans:
        cid = ch.get("category_id")
        groups.setdefault(cid, []).append(ch)
    lines = []
    for cid in sorted(groups):
        lines.append(f"{cats.get(cid, '其他')},#genre#")
        for ch in groups[cid]:
            srcs = ch.get("sources") or []
            if not srcs:
                continue
            lines.append(f"{ch['name']},{srcs[0]['url']}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"✓ 已生成 {path}: {len(chans)} 频道 / {len(groups)} 分组")


# ============================================================
# 自测（独立运行: python3 zbc4k_spider.py test）
# ============================================================
def self_test():
    s = Spider()
    s.init({})
    print("=== homeContent ===")
    home = s.homeContent(True)
    print("分类:", [c["type_name"] for c in home["class"]])

    print("\n=== categoryContent 电影 P1 ===")
    cat = s.categoryContent("3", "1", None, {})
    for v in cat.get("list", [])[:5]:
        print(f"  {v['vod_id']} {v['vod_name']} [{v['vod_remarks']}]")

    if cat.get("list"):
        vid = cat["list"][0]["vod_id"]
        print(f"\n=== detailContent {vid} ===")
        det = s.detailContent([vid])
        vod = det["list"][0]
        print(f"  {vod['vod_name']} ({vod['vod_year']}) {vod['vod_remarks']}")
        print(f"  演员: {vod['vod_actor'][:40]}")
        print(f"  播放源: {vod['vod_play_from']}")
        first = vod["vod_play_url"].split("$$$")[0].split("#")[0]
        print(f"  首集: {first[:80]}")

    print("\n=== 分类深翻页验证（电影 total） ===")
    c1 = s.categoryContent("3", "1", None, {})
    print(f"电影 P1: {len(c1['list'])} 部 / 总页数 {c1['total']}")
    cp = s.categoryContent("3", str(c1["total"]), None, {})
    print(f"电影 末页{c1['total']}: {len(cp.get('list', []))} 部（应为>0）")

    print("\n=== searchContent 斗罗大陆 ===")
    sr = s.searchContent("斗罗大陆", "")
    for v in sr.get("list", [])[:3]:
        print(f"  {v['vod_id']} {v['vod_name']} [{v['vod_remarks']}]")

    print("\n=== 直播（单项+分组筛选） ===")
    home2 = s.homeContent(True)
    print("分类:", [c["type_name"] for c in home2["class"]])
    lv_filter = home2.get("filters", {}).get("live", [])
    if lv_filter:
        print("筛选:", [v["n"] for v in lv_filter[0]["value"]])
    allc = s.categoryContent("live", "1", None, {})
    print(f"全部频道 P1: {len(allc['list'])} / 总页数 {allc['total']}")
    ysc = s.categoryContent("live", "1", None, {"group": "1"})
    print(f"筛选央视: {len(ysc['list'])} 频道")
    if ysc["list"]:
        det = s.detailContent([ysc["list"][0]["vod_id"]])
        lv = det["list"][0]
        print(f"  首个: {lv['vod_name']} 线路: {lv['vod_play_from']}")
        url = lv["vod_play_url"].split("$$$")[0].split("$", 1)[1]
        print(f"  播放: {url[:70]}")
    print("\n✓ 自测完成")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "live":
        gen_live()
    else:
        self_test()
