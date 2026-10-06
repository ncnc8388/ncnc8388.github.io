# coding=utf-8
"""
目标站: 听听FM (tingyou.fm)
终极修复版:
1. 【真·搜书修复】全面支持 album_title/album_name/album_id 字段变体，打通动态路由与原生接口；
2. 【播放双保险】优先 XChaCha20 / AES-GCM 官方接口秒解直链 (parse: 0)，失败自动降级 WebView 嗅探 (parse: 1)；
3. 【全量分类】保留最全的有声小说与评书名家细分专区。
"""
import re, sys, json, urllib.parse, hashlib, time, random, os
from bs4 import BeautifulSoup
import requests

sys.path.append('..')
from base.spider import Spider

try:
    from Crypto.Cipher import AES, ChaCha20_Poly1305
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


class Spider(Spider):

    def init(self, extend=""):
        self.site_url = "https://tingyou.fm"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': self.site_url + '/',
            'Origin': self.site_url,
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        
        self.categories = [
            {"type_id": "46", "type_name": "有声小说"},
            {"type_id": "1", "type_name": "评书"}
        ]
        self.filters = {
            "46": [
                {"key": "subtype", "name": "类型", "value": [
                    {"n": "全部", "v": ""}, {"n": "玄幻奇幻", "v": "46"}, {"n": "武侠小说", "v": "11"},
                    {"n": "言情通俗", "v": "19"}, {"n": "相声小品", "v": "21"}, {"n": "恐怖惊悚", "v": "14"},
                    {"n": "官场商战", "v": "17"}, {"n": "历史军事", "v": "15"}, {"n": "百家讲坛", "v": "9"},
                    {"n": "刑侦反腐", "v": "16"}, {"n": "有声文学", "v": "10"}, {"n": "人物纪实", "v": "18"},
                    {"n": "广播剧", "v": "36"}, {"n": "英文读物", "v": "22"}, {"n": "轻音清心", "v": "23"},
                    {"n": "二人转", "v": "31"}, {"n": "健康养生", "v": "33"}, {"n": "综艺娱乐", "v": "34"},
                    {"n": "头条", "v": "40"}, {"n": "戏曲", "v": "38"}, {"n": "脱口秀", "v": "41"},
                    {"n": "商业财经", "v": "42"}, {"n": "亲子教育", "v": "43"}, {"n": "教育培训", "v": "44"},
                    {"n": "时尚生活", "v": "45"}, {"n": "童话寓言", "v": "20"}
                ]},
                {"key": "sort", "name": "排序", "value": [
                    {"n": "综合排序", "v": "comprehensive"}, {"n": "播放最多", "v": "popular"},
                    {"n": "最近更新", "v": "updated"}, {"n": "最新发布", "v": "new"}
                ]}
            ],
            "1": [
                {"key": "subtype", "name": "类型", "value": [
                    {"n": "全部", "v": ""}, {"n": "单田芳", "v": "1"}, {"n": "刘兰芳", "v": "2"},
                    {"n": "田连元", "v": "3"}, {"n": "袁阔成", "v": "4"}, {"n": "连丽如", "v": "5"},
                    {"n": "孙一", "v": "8"}, {"n": "张少佐", "v": "6"}, {"n": "田战义", "v": "7"},
                    {"n": "周建龙", "v": "13"}, {"n": "马长辉", "v": "25"}, {"n": "王玥波", "v": "28"},
                    {"n": "粤语评书", "v": "12"}, {"n": "其他评书", "v": "13"}
                ]},
                {"key": "sort", "name": "排序", "value": [
                    {"n": "综合排序", "v": "comprehensive"}, {"n": "播放最多", "v": "popular"},
                    {"n": "最近更新", "v": "updated"}, {"n": "最新发布", "v": "new"}
                ]}
            ]
        }

        self.PAYLOAD_KEY_HEX = "ea9d9d4f9a983fe6f6382f29c7b46b8d6dc47abc6da36662e6ddff8c78902f65"
        self.PAYLOAD_VERSION = 1
        self.HARDCODED_AUTH = "Bearer gAAAAABpxTyveIsV3svITKMLKF6NdvuVhbJzxnWPJFmeav8M502s6toC4ryey8_DGOVK62SyVzJ1eDpcYA7Snr8kkcp5V40NaDyAudniva8y-Ac7MOBxPS9Ly1hlXxJ86s3xO9eg8HW9OPtoPIAIVJu19MSWo52zlVLeBlMBO903FQ-ZJBCVZuZdzg_Cok1d1_-C819LqDAfh_RzkMQmzBYxa6yCnhh_VImejRNaqSyb8sNYf-zYl009OaDGLNG8srEhaix7sVlN55n_9lhoxEVontCRN8rdaA=="
        self.HARDCODED_COOKIE = "dfp=f-c28yu:f-FTCFtTJZeXVY2UuWHmawNVQqrdGrZPVkiLIbYEqzXTnAgPfIngVZ4rn1sO+Y0AaxEryBUXuyhA5JUyUw7x0NcW/UEhTsbrYcpf30YWJPMcuN/edHp0T/fMMcMC07yROtEupjp6qCgfAZkU7zlDvWRx3cGG90tcQvvMXkEiCm4qKaq8zTTCTIAKeWjVdIjzis"

        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self._refresh_auth()

    # ================== 鉴权模块 ==================
    def _refresh_auth(self):
        try:
            anon = self._anonymous_auth()
            if anon.get('auth_token'): self.session.headers['Authorization'] = anon['auth_token']
            if anon.get('cookie'): self.session.headers['Cookie'] = anon['cookie']
        except Exception:
            self.session.headers['Authorization'] = self.HARDCODED_AUTH
            self.session.headers['Cookie'] = self.HARDCODED_COOKIE

    def _anonymous_auth(self):
        dfp = self._make_dfp_cookie()
        resp = self.session.post(self.site_url + '/api/me', headers={'Accept': 'application/json', 'Cookie': dfp['cookie']}, timeout=8)
        data = resp.json()
        token = data.get('auth_token', '')
        return {'auth_token': 'Bearer ' + token if token else '', 'cookie': dfp['cookie']}

    def _make_dfp_cookie(self, seed=""):
        d = time.localtime()
        time_factor = f"{d.tm_year}{d.tm_mon:02d}{d.tm_mday:02d}"
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        num = int(time_factor)
        base36 = ""
        while num > 0:
            base36 = chars[num % 36] + base36
            num //= 36
        fp_seed = seed or f"{int(time.time()*1000)}|tingyou|{random.random()}"
        fingerprint = hashlib.sha256(fp_seed.encode()).hexdigest()
        return {"cookie": f"dfp=f-{base36 or '0'}:f-{fingerprint}"}

    # ================== 加解密模块 ==================
    @staticmethod
    def _hex_to_bytes(s): return bytes.fromhex(s.strip())
    @staticmethod
    def _bytes_to_hex(b): return b.hex()

    def _encrypt_payload(self, plain_text):
        key = self._hex_to_bytes(self.PAYLOAD_KEY_HEX)
        nonce = os.urandom(12)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        ct, tag = cipher.encrypt_and_digest(plain_text.encode('utf-8'))
        return self._bytes_to_hex(bytearray([self.PAYLOAD_VERSION]) + nonce + ct + tag)

    def _decrypt_payload_hex(self, hex_str):
        raw = self._hex_to_bytes(hex_str)
        if len(raw) < 29: raise ValueError("payload too short")
        ver = raw[0]
        key = self._hex_to_bytes(self.PAYLOAD_KEY_HEX)

        if ver == 1:
            for n_start in (0, 1, 2):
                c_start = n_start + 12
                if c_start + 16 > len(raw): continue
                try:
                    nonce = raw[n_start:n_start+12]
                    rest = raw[c_start:]
                    tag = rest[-16:]
                    ct = rest[:-16]
                    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
                    return cipher.decrypt_and_verify(ct, tag).decode('utf-8')
                except Exception:
                    pass

        if len(raw) >= 41:
            nonce = raw[1:25]
            ciphertext = raw[25:]
            try:
                cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
                return cipher.decrypt_and_verify(ciphertext[:-16], ciphertext[-16:]).decode()
            except Exception:
                try:
                    rev = ciphertext[::-1]
                    cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
                    return cipher.decrypt_and_verify(rev[:-16], rev[-16:]).decode()
                except Exception:
                    pass

        raise ValueError("unable to decrypt")

    def _api_post(self, name_or_path, body_obj):
        path = name_or_path if name_or_path.startswith('/') else '/api/' + name_or_path
        url = self.site_url + path
        headers = {
            'Content-Type': 'text/plain',
            'X-Payload-Version': str(self.PAYLOAD_VERSION),
            'Accept': 'application/json',
            **self.session.headers
        }
        encrypted_body = self._encrypt_payload(json.dumps(body_obj))
        resp = self.session.post(url, data=encrypted_body, headers=headers, timeout=8)
        if resp.status_code != 200:
            return None

        raw_text = resp.text
        data = None
        if re.fullmatch(r'[0-9a-fA-F]+', raw_text) and len(raw_text) > 32:
            try:
                decrypted_text = self._decrypt_payload_hex(raw_text)
                data = json.loads(decrypted_text)
            except Exception:
                try: data = json.loads(raw_text)
                except: pass
        else:
            try: data = json.loads(raw_text)
            except: pass

        if isinstance(data, dict) and 'payload' in data:
            try:
                inner_plain = self._decrypt_payload_hex(data['payload'])
                data = json.loads(inner_plain)
            except Exception:
                pass
        return data

    # ================== 全兼容解析提取器 (核心重构) ==================
    def _get_val(self, raw, idx):
        if isinstance(idx, int) and 0 <= idx < len(raw):
            val = raw[idx]
            if isinstance(val, (str, int, float, bool)) or val is None:
                return val
        return idx

    def _parse_album_dict(self, item, raw=None):
        """全面兼容所有可能的键名变体，杜绝漏抓"""
        if not isinstance(item, dict): return None

        # 1. 查找 ID
        vid = None
        for k in ['id', 'album_id', 'albumId', 'albumID', 'vod_id', 'book_id']:
            if k in item:
                val = self._get_val(raw, item[k]) if raw else item[k]
                if val and str(val).strip().isdigit():
                    vid = str(val).strip()
                    break
        if not vid or len(vid) < 3: return None

        # 2. 查找标题（涵盖网站真实采用的 album_title / album_name）
        vname = None
        for k in ['title', 'album_title', 'albumTitle', 'name', 'album_name', 'albumName', 'vod_name', 'book_name']:
            if k in item:
                val = self._get_val(raw, item[k]) if raw else item[k]
                if val and str(val).strip() and str(val) != 'None':
                    vname = str(val).strip()
                    break
        if not vname: return None

        # 3. 查找封面
        vpic = ''
        for k in ['cover_url', 'coverUrl', 'cover', 'pic', 'image', 'album_cover', 'vod_pic']:
            if k in item:
                val = self._get_val(raw, item[k]) if raw else item[k]
                if val and str(val).strip() and str(val) != 'None':
                    vpic = str(val).strip()
                    break
        if vpic.startswith('//'): vpic = 'https:' + vpic
        elif vpic.startswith('/'): vpic = self.site_url + vpic

        # 4. 查找播音 / 作者 / 备注
        remarks = ''
        for k in ['teller', 'author', 'artist', 'anchor', 'vod_actor']:
            if k in item:
                val = self._get_val(raw, item[k]) if raw else item[k]
                if val and str(val).strip() and str(val) != 'None':
                    remarks = str(val).strip()
                    break
        if not remarks and 'status' in item:
            val = str(self._get_val(raw, item['status']) if raw else item['status'])
            remarks = '已完结' if (val == '0' or '完' in val) else '连载中'

        return {
            'vod_id': vid,
            'vod_name': vname,
            'vod_pic': vpic,
            'vod_remarks': remarks
        }

    def _extract_from_nuxt_payload(self, raw):
        """扫描 Nuxt 数据列表提取所有专辑"""
        videos = []
        seen = set()
        for item in raw:
            if isinstance(item, dict):
                album = self._parse_album_dict(item, raw)
                if album and album['vod_id'] not in seen:
                    videos.append(album)
                    seen.add(album['vod_id'])
        return videos

    def _find_albums_recursive(self, obj, seen, results, depth=0):
        """递归解析 JSON 树形对象"""
        if depth > 10: return
        if isinstance(obj, dict):
            album = self._parse_album_dict(obj)
            if album and album['vod_id'] not in seen:
                results.append(album)
                seen.add(album['vod_id'])
            for v in obj.values():
                self._find_albums_recursive(v, seen, results, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                self._find_albums_recursive(item, seen, results, depth + 1)

    def _parse_html_list(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.select('a[href*="/albums/"]')
        video_list = []
        seen = set()
        for a in cards:
            href = a.get('href', '')
            aid = href.split('/albums/')[-1].split('?')[0].strip('/')
            if not aid or aid in seen or not aid.isdigit(): continue

            title_tag = a.select_one('p.title, .title, h2, h3, p')
            name = title_tag.get_text(strip=True) if title_tag else ""
            if not name: continue

            img_tag = a.select_one('img')
            pic = ''
            if img_tag:
                src = img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or ''
                if src.startswith('http'): pic = src
                elif src.startswith('//'): pic = 'https:' + src
                elif src.startswith('/'): pic = self.site_url + src

            remarks = ''
            desc_tag = a.select_one('p.desc, .desc, .author')
            if desc_tag: remarks = desc_tag.get_text(strip=True)

            video_list.append({'vod_id': aid, 'vod_name': name, 'vod_pic': pic, 'vod_remarks': remarks})
            seen.add(aid)
        return video_list

    # ================== 页面与分类 ==================
    def homeContent(self, filter):
        url = self.site_url + "/"
        resp = self.fetch(url, headers=self.headers)
        if not resp or not resp.text:
            return {"class": self.categories, "list": [], "filters": self.filters}

        videos = []
        soup = BeautifulSoup(resp.text, 'html.parser')
        tag = soup.find('script', id='__NUXT_DATA__')
        if tag and tag.string:
            try:
                raw = json.loads(tag.string)
                videos = self._extract_from_nuxt_payload(raw)
            except: pass

        if not videos:
            videos = self._parse_html_list(resp.text)

        return {"class": self.categories, "list": videos[:24], "filters": self.filters}

    def homeVideoContent(self):
        return self.homeContent(False)

    def categoryContent(self, tid, pg, filter, extend):
        page = int(pg) if pg else 1
        sort = extend.get("sort", "comprehensive") if extend else "comprehensive"
        subtype = extend.get("subtype", "") if extend else ""
        if subtype: tid = subtype

        url = f"{self.site_url}/categories/{tid}?page={page}&sort={sort}"
        resp = self.fetch(url, headers=self.headers)
        if not resp or not resp.text:
            return {"list": [], "page": page, "pagecount": page, "limit": 24, "total": 0}

        soup = BeautifulSoup(resp.text, 'html.parser')
        tag = soup.find('script', id='__NUXT_DATA__')
        videos = []
        if tag and tag.string:
            try:
                raw = json.loads(tag.string)
                videos = self._extract_from_nuxt_payload(raw)
            except: pass

        if not videos:
            videos = self._parse_html_list(resp.text)

        pagecount = page + 1 if len(videos) > 0 else page
        return {"list": videos, "page": page, "pagecount": pagecount, "limit": 24, "total": 0}

    def detailContent(self, ids):
        if not ids: return {"list": []}
        vod_id = ids[0]
        url = f"{self.site_url}/albums/{vod_id}"
        resp = self.fetch(url, headers=self.headers)
        if not resp or resp.status_code != 200: return {"list": []}

        soup = BeautifulSoup(resp.text, 'html.parser')
        title_tag = soup.select_one('h1.title') or soup.select_one('.album-intro h1')
        vod_name = title_tag.get_text(strip=True) if title_tag else vod_id

        vod_pic = ''
        img_tag = soup.select_one('img.cover-img, img.cover')
        if img_tag:
            src = img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or ''
            if src.startswith('http'): vod_pic = src
            elif src.startswith('//'): vod_pic = 'https:' + src
            elif src.startswith('/'): vod_pic = self.site_url + src

        intro_div = soup.select_one('div.album-intro')
        intro_text = intro_div.get_text(' ', strip=True) if intro_div else ''
        vod_area = vod_actor = vod_director = vod_remarks = ''

        for pattern, attr in [('分类[：:]\s*([^\s]+)', 'area'), ('作者[：:]\s*([^\s]+)', 'director'),
                              ('播音[：:]\s*([^\s]+)', 'actor'), ('状态[：:]\s*([^\s]+)', 'remarks')]:
            m = re.search(pattern, intro_text)
            if m:
                val = m.group(1)
                if attr == 'area': vod_area = val
                elif attr == 'director': vod_director = val
                elif attr == 'actor': vod_actor = val
                elif attr == 'remarks': vod_remarks = val

        episodes = []
        chapter_list = soup.select('ul.chapter-list li')
        if chapter_list:
            for idx, li in enumerate(chapter_list, 1):
                title_div = li.select_one('div.item-content p.title') or li.select_one('.title')
                ep_name = title_div.get_text(strip=True) if title_div else f"第{idx}集"

                chapter_id = li.get('data-id', '') or li.get('data-chapter-id', '')
                if not chapter_id:
                    a_tag = li.select_one('a')
                    if a_tag and a_tag.get('href') and 'chapterId=' in a_tag['href']:
                        chapter_id = a_tag['href'].split('chapterId=')[-1].split('&')[0]
                if not chapter_id:
                    chapter_id = str(idx)

                episodes.append(f"{ep_name}${vod_id}|{chapter_id}")

        if not episodes:
            episodes.append(f"播放${vod_id}|1")

        return {"list": [{
            "vod_id": vod_id, "vod_name": vod_name, "vod_pic": vod_pic,
            "vod_content": intro_text, "vod_actor": vod_actor,
            "vod_director": vod_director, "vod_area": vod_area,
            "vod_remarks": vod_remarks,
            "vod_play_from": '听友FM',
            "vod_play_url": '#'.join(episodes)
        }]}

    # ================== 核心：多通道穿透式搜索 ==================
    def searchContent(self, key, quick, pg="1"):
        if not key or not key.strip():
            return {"list": [], "page": 1, "pagecount": 1, "total": 0}

        page = int(pg) if pg else 1
        clean_key = key.strip()
        lower_key = clean_key.lower()
        encoded_key = urllib.parse.quote(clean_key)

        matched_books = []
        seen = set()

        # ---------- 通道 1: 官方加密 API 直查 (若开放直接拿真数据) ----------
        if HAS_CRYPTO:
            for endpoint in ["search", "album/search", "albums"]:
                for param_name in ["keyword", "q", "wd"]:
                    try:
                        data = self._api_post(endpoint, {param_name: clean_key, "page": page})
                        if data:
                            self._find_albums_recursive(data, seen, matched_books)
                            if matched_books: break
                    except Exception:
                        pass
                if matched_books: break

        # ---------- 通道 2: 官方 GET API 直查 ----------
        if not matched_books:
            for api_url in [
                f"{self.site_url}/api/search?keyword={encoded_key}&page={page}",
                f"{self.site_url}/api/search?q={encoded_key}&page={page}",
                f"{self.site_url}/api/albums?keyword={encoded_key}&page={page}"
            ]:
                try:
                    resp = self.fetch(api_url, headers=self.headers)
                    if resp and resp.status_code == 200:
                        data = json.loads(resp.text)
                        self._find_albums_recursive(data, seen, matched_books)
                        if matched_books: break
                except Exception:
                    pass

        # ---------- 通道 3: 官方页面探测 (支持动态路由与传统查询) ----------
        if not matched_books:
            candidate_pool = []
            seen_cand = set()
            
            # 测试 动态路径 与 查询传参 两个入口
            for page_url in [
                f"{self.site_url}/search/{encoded_key}?page={page}",
                f"{self.site_url}/search?keyword={encoded_key}&page={page}",
                f"{self.site_url}/search?q={encoded_key}&page={page}"
            ]:
                try:
                    resp = self.fetch(page_url, headers=self.headers)
                    if not resp or not resp.text: continue

                    # 1. 扫描 Nuxt 数据块（全字段识别 album_title / album_name）
                    tag = BeautifulSoup(resp.text, 'html.parser').find('script', id='__NUXT_DATA__')
                    if tag and tag.string:
                        try:
                            raw = json.loads(tag.string)
                            for v in self._extract_from_nuxt_payload(raw):
                                if v['vod_id'] not in seen_cand:
                                    candidate_pool.append(v)
                                    seen_cand.add(v['vod_id'])
                        except Exception:
                            pass

                    # 2. HTML DOM 提取
                    for v in self._parse_html_list(resp.text):
                        if v['vod_id'] not in seen_cand:
                            candidate_pool.append(v)
                            seen_cand.add(v['vod_id'])

                    if candidate_pool: break
                except Exception:
                    continue

            # 智能相关性筛选（只保留真命中的书，杜绝推荐乱码）
            for item in candidate_pool:
                # 命中书名
                if lower_key in item['vod_name'].lower():
                    if item['vod_id'] not in seen:
                        matched_books.append(item)
                        seen.add(item['vod_id'])

            for item in candidate_pool:
                # 命中播音/作者
                if lower_key in item.get('vod_remarks', '').lower():
                    if item['vod_id'] not in seen:
                        matched_books.append(item)
                        seen.add(item['vod_id'])

            # 宽容兜底：如果关键词有繁简或分词微差，只要抓回来的不是满页20本大杂烩，放行真实搜索项
            if not matched_books and 0 < len(candidate_pool) <= 6:
                matched_books = candidate_pool

        pagecount = page + 1 if len(matched_books) > 0 else page
        return {
            "list": matched_books,
            "page": page,
            "pagecount": pagecount,
            "limit": 24,
            "total": len(matched_books)
        }

    # ================== 播放解析 ==================
    def playerContent(self, flag, id, vipFlags):
        parts = re.split(r'[\|\$]', str(id))
        album_id = parts[0]
        chapter_idx = parts[1] if len(parts) > 1 else "1"

        # 方案一：通过官方解密 API 直取音频直链 (parse: 0 秒播)
        if HAS_CRYPTO:
            try:
                c_idx_int = int(re.sub(r'\D', '', chapter_idx)) if re.sub(r'\D', '', chapter_idx) else 1
                data = self._api_post("play_token", {"album_id": int(album_id), "chapter_idx": c_idx_int})

                def _find_audio_url(obj, depth=0):
                    if depth > 10: return None
                    if isinstance(obj, str) and obj.startswith('http') and ('.mp3' in obj or '.m4a' in obj or '.aac' in obj or 'sign=' in obj):
                        return obj
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if isinstance(v, str) and v.startswith('http') and re.search(r'(url|src|play|audio|file|link)', k, re.I):
                                return v
                            res = _find_audio_url(v, depth+1)
                            if res: return res
                    elif isinstance(obj, list):
                        for item in obj:
                            res = _find_audio_url(item, depth+1)
                            if res: return res
                    return None

                play_url = _find_audio_url(data)
                if play_url:
                    return {
                        "parse": 0,
                        "url": play_url,
                        "header": self.headers
                    }
            except Exception:
                pass

        # 方案二：解密失败时的原生网页嗅探兜底 (parse: 1)
        fallback_url = f"{self.site_url}/playing?albumId={album_id}&chapterId={chapter_idx}"
        return {
            "parse": 1,
            "url": fallback_url,
            "header": self.headers
        }