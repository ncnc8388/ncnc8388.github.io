"""
@header({
  searchable: 1,
  filterable: 1,
  quickSearch: 1,
  title: 'bili_open2',
  lang: 'hipy'
})
"""

# 哔哩哔哩（bili2）—— 由同目录 bili2.js（CatVod/quickjs 版）移植的 Python 源。
# 功能对照：首页推荐 / 分区（关键词搜索）/ 历史记录 / 详情（多清晰度、多分P、相关推荐）/
#          搜索 / 播放（dash MPD、mp4 直链、dash 外挂音频）/ dash 代理（localProxy）。
# 与原 JS 版一致：dash 的 MPD 里 BaseURL 是 B 站 CDN 直链，客户端直接拉流。

import base64
import hashlib
import json
import random
import re
import time
from urllib.parse import quote, urlencode

from base.spider import BaseSpider

API_NAV = 'https://api.bilibili.com/x/web-interface/nav'
API_VIEW = 'https://api.bilibili.com/x/web-interface/view'
API_PLAYURL = 'https://api.bilibili.com/x/player/wbi/playurl'
API_PLAYURL_PLAIN = 'https://api.bilibili.com/x/player/playurl'
API_RCMD = 'https://api.bilibili.com/x/web-interface/index/top/rcmd'
API_POPULAR = 'https://api.bilibili.com/x/web-interface/popular'
API_SEARCH = 'https://api.bilibili.com/x/web-interface/wbi/search/type'
API_RELATED = 'https://api.bilibili.com/x/web-interface/archive/related'
API_HISTORY = 'https://api.bilibili.com/x/v2/history'
API_HISTORY_REPORT = 'https://api.bilibili.com/x/v2/history/report'
API_DANMAKU = 'https://api.bilibili.com/x/v1/dm/list.so?oid='


class Spider(BaseSpider):
    # 默认分类：每个分类名会被当作 B 站搜索关键词（与 bili2.js 的 extend 一致）
    DEFAULT_CATEGORIES = '国语配音#国配电影#短剧#国配电视剧#国配动漫#台配国语#台配电影#台配电视剧#台配动漫#国语配音动漫'

    # 登录 Cookie：与 bili2.js 里写死的值一致，换成自己的账号 Cookie 可以解锁更高清晰度
    DEFAULT_COOKIE = (
        'buvid3=693EC465-610F-7CAB-3EBA-D2B68AF75E7874446infoc; '
        'buvid_fp=4455de7aad0c3617d90e9b5bb74cc5da; '
        'SESSDATA=75f0f233%2C1806986691%2Cde633%2Aa2CjDfGXUU2NbgAyx9f6CzlsXxqha-zGq_NAGQawDckp47HfqbDV5ZibHTzqvF0zC98kISVmQyeUVFNm9TZk8tRjloQmxFenBOYWM5WWdFV29sQkpfTzZ0TEp4cDU4VnEwenpwaVVoTFREUUdJUG9ydFhCanpCdm80UEJIdWNOSllNX0tXR1RVR0xnIIEC; '
        'bili_jct=f492499ef4c29528aa6cad81cad4bae1'
    )

    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.bilibili.com',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }

    PLAY_HEADERS = {
        'Referer': 'https://www.bilibili.com',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
    }

    # dash 音频 id -> 采样率
    AUDIO_BITRATE = {30280: 192000, 30232: 132000, 30216: 64000}

    # dash 视频 codecid -> 编码名
    CODEC_NAME = {13: 'AV1', 12: 'HEVC', 7: 'AVC'}

    # dash 分片是否走服务端代理：
    #   0 = 直链（与 bili2.js 一致，客户端直连 B 站 CDN，省服务器带宽）
    #   3 = 服务端内联流式转发（带 Referer/UA，规避部分 CDN 节点 403，但占用服务器带宽）
    # 直链播放失败时可以改成 3 试试。
    DASH_MEDIA_PROXY = 0

    # 播放线路名（detailContent 里 vod_play_from 的顺序）
    LINE_DASH = 'dash'
    LINE_MP4 = 'mp4'
    LINE_DASH_AUDIO = 'dash1'
    LINE_RELATED = '相关'

    # wbi 签名混淆表（与 JS 版一致）
    WBI_KEY_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9,
        42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1,
        60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]

    def __init__(self, query_params=None, t4_api=None):
        super().__init__(query_params=query_params, t4_api=t4_api)
        self.categories = []
        self.cookie = self.DEFAULT_COOKIE
        self.bili_jct = ''
        self.is_login = False
        self.is_vip = False
        self._wbi_mixin_key = ''
        self._nav_time = 0
        self.localProxyUrl = self._build_proxy_prefix(self.getProxyUrl())
        self._parse_cookie(self.cookie)

    # ------------------------------------------------------------------ 初始化

    @staticmethod
    def _build_proxy_prefix(proxy_url):
        """把 getProxyUrl()（如 http://host/proxy/hipy_py_bili2/?do=py&extend=）拼成本地代理前缀。

        站点 key 直接从地址里取，复制/改名源文件后不用再手工改。
        """
        if not proxy_url:
            return ''
        if 'hikerSkey' in proxy_url:
            return proxy_url + '&type='
        matched = re.search(r'/proxy/([^/?]+)', proxy_url)
        site_key = matched.group(1) if matched else 'hipy_py_bili2'
        separator = '&' if '?' in proxy_url else '?'
        return f'{proxy_url}{separator}siteType=3&siteKey={site_key}&type='

    def init(self, extend=''):
        """分类来自扩展参数（# 分隔），为空则用默认分类。

        注意：drpy-node 的 Python 守护进程调用 init 时传的是「依赖模块列表」，
        真正的扩展参数在 setExtendInfo() 里，所以这里两种来源都兼容。
        """
        raw = extend if isinstance(extend, str) else ''
        if not raw.strip() and isinstance(self.extend, str):
            raw = self.extend
        raw = raw.strip()
        if raw.startswith(('{', '[')):
            raw = ''
        categories = [x.strip() for x in re.split(r'[#\n]', raw) if x.strip()] if raw else []
        self.categories = categories or self.DEFAULT_CATEGORIES.split('#')

    def destroy(self):
        pass

    def isVideoFormat(self, url):
        pass

    def manualVideoCheck(self):
        pass

    # ------------------------------------------------------------------ 小工具

    @staticmethod
    def _b64(text):
        """URL 安全的 base64（去掉 = 补位），可直接放进查询参数。"""
        return base64.urlsafe_b64encode(str(text).encode('utf-8')).decode('ascii').rstrip('=')

    @staticmethod
    def _unb64(text):
        text = str(text or '')
        return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4)).decode('utf-8')

    @staticmethod
    def _strip_tags(text):
        return re.sub(r'<[^>]*>', '', str(text or ''))

    @staticmethod
    def _safe_title(text):
        """标题里避开 drpy 播放串的分隔符。"""
        return str(text or '').replace('$', '＄').replace('#', '＃')

    @staticmethod
    def _duration_text(value):
        """把 "12:34" 或秒数转成 "12分钟 34秒" 形式的备注。"""
        text = str(value if value is not None else '')
        total = None
        try:
            if ':' in text:
                parts = [int(x) for x in text.split(':')]
                if len(parts) >= 3:
                    total = parts[0] * 3600 + parts[1] * 60 + parts[2]
                elif len(parts) == 2:
                    total = parts[0] * 60 + parts[1]
                else:
                    total = parts[0]
            else:
                total = int(float(text))
        except (TypeError, ValueError):
            return '无效输入'
        if total >= 3600:
            return f'{total // 3600}小时 {total % 3600 // 60}分钟 {total % 60}秒'
        return f'{total // 60}分钟 {total % 60}秒'

    @staticmethod
    def _raw_url(media):
        """dash 媒体直链（原样返回，供播放器直接使用）。"""
        return str(media.get('baseUrl') or media.get('base_url') or '')

    def _dash_base_url(self, raw_url):
        """MPD 里的分片地址：直链，或包一层 localProxy（DASH_MEDIA_PROXY 打开时）。"""
        if not self.DASH_MEDIA_PROXY or not self.localProxyUrl or not raw_url:
            return raw_url
        return f'{self.localProxyUrl}media&url={self._b64(raw_url)}'

    def _audio_bitrate(self, media_id):
        """dash 音频 id -> 采样率，兼容字符串形式的 id。"""
        try:
            return self.AUDIO_BITRATE.get(int(media_id), 0)
        except (TypeError, ValueError):
            return 0

    def _name_of(self, encoded_names, index):
        if index >= len(encoded_names):
            return ''
        try:
            return self._unb64(encoded_names[index])
        except Exception:
            return encoded_names[index]

    # ------------------------------------------------------------------ 网络与签名

    def _headers(self):
        headers = dict(self.HEADERS)
        if self.cookie:
            headers['Cookie'] = self.cookie
        return headers

    def _get_json(self, url, params=None, headers=None, wbi=False, timeout=30):
        if wbi:
            url += ('&' if '?' in url else '?') + self._wbi_sign(params or {})
        elif params:
            query = urlencode({k: v for k, v in params.items() if v is not None})
            url += ('&' if '?' in url else '?') + query
        response = self.fetch(url, headers=headers or self._headers(), timeout=timeout)
        return response.json()

    def _parse_cookie(self, cookie):
        self.cookie = cookie or ''
        self.bili_jct = ''
        for part in self.cookie.split(';'):
            part = part.strip()
            if part.startswith('bili_jct='):
                self.bili_jct = part.split('=', 1)[1]
                break

    def _load_wbi_key(self):
        data = self._get_json(API_NAV, headers=self._headers()).get('data') or {}
        wbi_img = data.get('wbi_img') or {}
        img_key = (wbi_img.get('img_url') or '').rsplit('/', 1)[-1].split('.')[0]
        sub_key = (wbi_img.get('sub_url') or '').rsplit('/', 1)[-1].split('.')[0]
        if not img_key or not sub_key:
            raise RuntimeError('获取 wbi 密钥失败（nav 接口异常）')
        combined = img_key + sub_key
        self._wbi_mixin_key = ''.join(combined[i] for i in self.WBI_KEY_TAB)[:32]

    def _wbi_sign(self, params):
        """B 站 wbi 签名，返回可直接拼在 URL 后面的查询串。"""
        if not self._wbi_mixin_key:
            self._load_wbi_key()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params['wts'] = round(time.time())
        params['dm_img_list'] = '[]'
        params['dm_img_str'] = ''.join(random.choice('ABCDEFGHIJK') for _ in range(2))
        params['dm_cover_img_str'] = ''.join(random.choice('ABCDEFGHIJK') for _ in range(2))
        params['dm_img_inter'] = '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'
        query = '&'.join(f'{key}={quote(str(params[key]), safe="")}' for key in sorted(params))
        w_rid = hashlib.md5((query + self._wbi_mixin_key).encode('utf-8')).hexdigest()
        return f'{query}&w_rid={w_rid}'

    def _ensure_nav(self, force=False):
        """补一次登录状态（每小时刷新），播放清晰度上限依赖它。"""
        if not force and time.time() - self._nav_time < 3600:
            return
        try:
            data = self._get_json(API_NAV, headers=self._headers()).get('data') or {}
            self.is_login = bool(data.get('isLogin'))
            self.is_vip = bool(data.get('vipStatus'))
        except Exception as e:
            self.log(f'bili2 获取登录状态失败: {e}')
        self._nav_time = time.time()

    # ------------------------------------------------------------------ 列表数据

    def _vod(self, item):
        """把 rcmd / 搜索 / 历史 的条目统一转成 vod 结构。"""
        picture = item.get('pic') or item.get('cover') or ''
        if picture.startswith('//'):
            picture = 'https:' + picture
        return {
            'vod_id': item.get('bvid') or '',
            'vod_name': self._strip_tags(item.get('title')),
            'vod_pic': picture,
            'vod_remarks': self._duration_text(item.get('duration')),
        }

    @staticmethod
    def _filter_config():
        """分类的筛选项（排序 / 时长），与 bili2.js 的 jsonData 一致。"""
        return [
            {
                'key': 'order',
                'name': '排序',
                'value': [
                    {'n': '综合排序', 'v': '0'},
                    {'n': '最多点击', 'v': 'click'},
                    {'n': '最新发布', 'v': 'pubdate'},
                    {'n': '最多弹幕', 'v': 'dm'},
                    {'n': '最多收藏', 'v': 'stow'},
                ],
            },
            {
                'key': 'duration',
                'name': '时长',
                'value': [
                    {'n': '全部时长', 'v': '0'},
                    {'n': '60分钟以上', 'v': '4'},
                    {'n': '30~60分钟', 'v': '3'},
                    {'n': '10~30分钟', 'v': '2'},
                    {'n': '10分钟以下', 'v': '1'},
                ],
            },
        ]

    # ------------------------------------------------------------------ 接口

    def homeContent(self, filter=1):
        classes = [{'type_name': '首页', 'type_id': '首页'}]
        classes += [{'type_name': name, 'type_id': name} for name in self.categories]
        if self.bili_jct:
            classes.append({'type_name': '历史记录', 'type_id': '历史记录'})
        result = {'class': classes}
        if filter:
            result['filters'] = {name: self._filter_config() for name in self.categories}
        return result

    def homeVideoContent(self):
        # fresh_idx 写死为 1 会永远返回同一批视频，随机翻一页推荐流
        videos = self._rcmd_videos(random.randint(1, 30))
        if not videos:
            # rcmd 被风控/无返回时退回热门榜单，保证首页有内容
            try:
                data = self._get_json(f'{API_POPULAR}?ps=20&pn=1').get('data') or {}
                videos = [self._vod(item) for item in (data.get('list') or []) if item.get('bvid')]
            except Exception as e:
                self.log(f'bili2 首页热门兜底失败: {e}')
        return {'list': videos}

    def _rcmd_videos(self, page):
        """首页推荐流，返回 vod 列表（带 simple 内层兼容）。"""
        data = self._get_json(f'{API_RCMD}?ps=14&fresh_idx={page}&fresh_idx_1h={page}').get('data') or {}
        items = data.get('item') or []
        return [self._vod(item) for item in items if item.get('bvid')]

    def _popular_videos(self, page):
        """热门榜单兜底（rcmd 被风控时用）。"""
        data = self._get_json(f'{API_POPULAR}?ps=20&pn={page}').get('data') or {}
        return [self._vod(item) for item in (data.get('list') or []) if item.get('bvid')]

    def categoryContent(self, tid, pg=1, filter=1, extend=None):
        page = int(pg or 1)
        if page < 1:
            page = 1
        extend = dict(extend or {})
        if extend.get('tid'):
            tid = extend['tid']
        extend.pop('tid', None)

        if tid == '首页':
            videos = self._rcmd_videos(page)
            if not videos:
                videos = self._popular_videos(page)
            page_count = page + 1
        elif tid == '历史记录':
            data = self._get_json(f'{API_HISTORY}?pn={page}').get('data')
            items = (data.get('list') if isinstance(data, dict) else data) or []
            page_count = page + 1
        else:
            params = {'search_type': 'video', 'keyword': tid, 'page': page}
            params.update(extend)
            data = self._get_json(API_SEARCH, params=params, wbi=True).get('data') or {}
            if not data.get('result'):
                # wbi 校验偶发失败时退回不带签名的请求，尽量别让分类空白
                data = self._get_json(API_SEARCH, params=params).get('data') or {}
            items = data.get('result') or []
            page_count = data.get('numPages') or (page + 1)

        if tid != '首页':
            videos = [self._vod(item) for item in items if item.get('bvid')]
        return {
            'page': page,
            'pagecount': page_count,
            'limit': len(videos),
            'total': 999999,
            'list': videos,
        }

    def searchContent(self, key, quick=0, pg=1):
        return self.categoryContent(key, pg, True, {'duration': '0'})

    def _quality_list(self, play_data):
        """按登录/VIP 状态过滤清晰度，返回 (清晰度 id 列表, base64 名称列表)。"""
        quality_ids = play_data.get('accept_quality') or []
        descriptions = play_data.get('accept_description') or []
        ids, names = [], []
        for index, qn in enumerate(quality_ids):
            if not self.is_vip:
                limit = 80 if self.is_login else 32
                if int(qn) > limit:
                    continue
            ids.append(str(qn))
            name = descriptions[index] if index < len(descriptions) else str(qn)
            names.append(self._b64(name))
        if not ids:
            # playurl 偶尔拿不到清晰度列表，给一档默认值，保证详情页仍有可播放线路
            ids, names = ['80'], [self._b64('高清 1080P')]
        return ids, names

    def detailContent(self, ids=''):
        # 框架传进来的是数组（api.js 里 query.ids.split(',')），这里两种形态都兼容
        if isinstance(ids, (list, tuple)):
            bvid = str(ids[0]).strip() if ids else ''
        else:
            bvid = str(ids or '').strip()
        bvid = bvid.strip("'\"[] ")
        if not bvid:
            return {'list': []}
        self._ensure_nav()
        data = self._get_json(f'{API_VIEW}?bvid={bvid}').get('data') or {}
        if not data:
            return {'list': []}

        aid = data.get('aid')
        cid = data.get('cid')
        self._report_history(aid, cid)

        play_data = self._get_json(
            f'{API_PLAYURL_PLAIN}?avid={aid}&cid={cid}&qn=127&fnval=4048&fourk=1'
        ).get('data') or {}
        quality_ids, quality_names = self._quality_list(play_data)

        # 分P：多P 用分P标题，单P 用视频标题
        pages = data.get('pages') or [{'cid': cid, 'part': data.get('title')}]
        pay_items = []
        for index, page_item in enumerate(pages):
            page_cid = page_item.get('cid')
            title = self._safe_title(page_item.get('part') or f'P{index + 1}')
            pay_items.append(
                f'{title}${aid}@{page_cid}@{":".join(quality_ids)}@{":".join(quality_names)}'
            )

        # 相关推荐（用同一个播放串格式，播放时按 "相关" 线路处理）
        related = self._get_json(f'{API_RELATED}?bvid={bvid}').get('data') or []
        related_items = []
        for item in related:
            if not item.get('cid'):
                continue
            title = self._safe_title(item.get('title'))
            related_items.append(
                f'{title}${item.get("aid")}@{item.get("cid")}@{":".join(quality_ids)}@{":".join(quality_names)}'
            )

        vod = {
            'vod_id': bvid,
            'vod_name': self._strip_tags(data.get('title')),
            'vod_pic': data.get('pic') or '',
            'type_name': data.get('tname') or '',
            'vod_year': '',
            'vod_area': '',
            'vod_remarks': self._duration_text(data.get('duration')),
            'vod_actor': '',
            'vod_director': '',
            'vod_content': data.get('desc') or '',
        }
        pay_urls = '#'.join(pay_items)
        vod['vod_play_from'] = '$$$'.join([
            self.LINE_DASH, self.LINE_MP4, self.LINE_DASH_AUDIO, self.LINE_RELATED,
        ])
        vod['vod_play_url'] = '$$$'.join([
            pay_urls, pay_urls, pay_urls, '#'.join(related_items),
        ])
        return {'list': [vod]}

    def _report_history(self, aid, cid):
        """登录状态下把当前视频写进 B 站历史（失败不影响详情返回）。"""
        if not self.bili_jct:
            return
        try:
            self.post(
                API_HISTORY_REPORT,
                data={'aid': aid, 'cid': cid, 'csrf': self.bili_jct},
                headers=self._headers(),
                timeout=5,
            )
        except Exception as e:
            self.log(f'bili2 上报历史失败: {e}')

    # ------------------------------------------------------------------ 播放

    def playerContent(self, flag, id, vipFlags=None):
        try:
            raw_id = str(id or '').replace(' ', '+')  # 客户端未编码时 qs 会把 + 解析成空格
            parts = raw_id.split('@') if '@' in raw_id else raw_id.split('+')
            if len(parts) < 4:
                return {}
            aid, cid = parts[0], parts[1]
            quality_ids = [x for x in parts[2].split(':') if x]
            quality_names = parts[3].split(':')
            line = str(flag or '').lower()
            if line == self.LINE_MP4:
                return self._play_mp4(aid, cid, quality_ids, quality_names)
            if line == self.LINE_DASH_AUDIO:
                return self._play_dash_audio(aid, cid, quality_ids, quality_names)
            # dash / 相关 / 其它线路：走 MPD（失败时客户端会回落到直链线路）
            return self._play_dash(aid, cid, quality_ids, quality_names)
        except Exception as e:
            self.log(f'bili2 播放失败: {e}')
            return {}

    def _play_dash(self, aid, cid, quality_ids, quality_names):
        """每个清晰度一个 MPD 地址，MPD 由 localProxy 现拼。"""
        if not self.localProxyUrl:
            return {}
        urls = []
        for index, qn in enumerate(quality_ids):
            payload = self._b64(f'{aid}@{cid}@{qn}')
            urls += [self._name_of(quality_names, index), f'{self.localProxyUrl}dash&url={payload}']
        return {
            'parse': 0,
            'url': urls,
            'danmaku': API_DANMAKU + str(cid),
            'header': dict(self.PLAY_HEADERS),
        }

    def _play_mp4(self, aid, cid, quality_ids, quality_names):
        """durl 直链（单文件 mp4）。"""
        urls = []
        for index, qn in enumerate(quality_ids):
            signed = self._wbi_sign({'avid': aid, 'cid': cid, 'qn': qn, 'fourk': 1})
            data = self._get_json(f'{API_PLAYURL}?{signed}').get('data') or {}
            if str(data.get('quality')) != str(qn):
                continue
            durl = (data.get('durl') or [{}])[0].get('url')
            if not durl:
                continue
            urls += [self._name_of(quality_names, index), durl]
        return {
            'parse': 0,
            'url': urls,
            'danmaku': API_DANMAKU + str(cid),
            'header': dict(self.PLAY_HEADERS),
        }

    def _play_dash_audio(self, aid, cid, quality_ids, quality_names):
        """视频/音频分离的 dash 直链，音频放在 extra.audio 里交给客户端。"""
        urls = []
        audios = []
        for index, qn in enumerate(quality_ids):
            data = self._get_json(
                f'{API_PLAYURL}?avid={aid}&cid={cid}&qn={qn}&fnval=4048&fourk=1'
            ).get('data') or {}
            dash = data.get('dash') or {}
            for video in dash.get('video') or []:
                if str(video.get('id')) != str(qn):
                    continue
                codec = self.CODEC_NAME.get(video.get('codecid'))
                if not codec:
                    continue
                urls += [f'{self._name_of(quality_names, index)} {codec}', self._raw_url(video)]
            if not audios:
                for audio in dash.get('audio') or []:
                    bit = self._audio_bitrate(audio.get('id'))
                    if bit:
                        audios.append({
                            'title': f'{bit // 1024}Kbps',
                            'bit': bit,
                            'url': self._raw_url(audio),
                        })
                audios.sort(key=lambda item: item['bit'])
        return {
            'parse': 0,
            'url': urls,
            'extra': {'audio': audios},
            'danmaku': API_DANMAKU + str(cid),
            'header': dict(self.PLAY_HEADERS),
        }

    # ------------------------------------------------------------------ dash 代理

    def localProxy(self, param, *args, **kwargs):
        param = param or {}
        try:
            if param.get('type') == 'dash':
                return self._proxy_dash(param.get('url') or '')
            if param.get('type') == 'media':
                return self._proxy_media(param.get('url') or '')
            return [404, 'text/plain; charset=utf-8', f'未知代理类型: {json.dumps(param, ensure_ascii=False)}']
        except Exception as e:
            self.log(f'bili2 代理失败: {e}')
            return [500, 'text/plain; charset=utf-8', str(e)]

    def _proxy_media(self, payload):
        """dash 分片中转：返回 CDN 直链 + 播放头，由服务端流式转发（支持 Range）。"""
        url = self._unb64(payload)
        if not url.startswith('http'):
            return [400, 'text/plain; charset=utf-8', 'media 参数不完整']
        return [200, 'application/octet-stream', url, dict(self.PLAY_HEADERS), 3]

    def _proxy_dash(self, payload):
        """把 (aid+cid+qn) 换成一份 DASH MPD；BaseURL 为 CDN 直链。"""
        raw = self._unb64(payload).replace(' ', '+')
        aid, cid, qn = ((raw.split('@') if '@' in raw else raw.split('+')) + ['', '', ''])[:3]
        if not (aid and cid and qn):
            return [400, 'text/plain; charset=utf-8', 'dash 参数不完整']

        signed = self._wbi_sign({
            'avid': aid, 'cid': cid, 'qn': qn,
            'fnval': 4048, 'fnver': 0, 'fourk': 1, 'from_client': 'BROWSER',
        })
        response = self._get_json(f'{API_PLAYURL}?{signed}')
        dash = (response.get('data') or {}).get('dash') or {}
        if not dash:
            content = json.dumps(
                {'code': response.get('code'), 'message': response.get('message'), 'url': signed},
                ensure_ascii=False,
            )
            return [500, 'application/json; charset=utf-8', content]

        # 每个清晰度只取一个编码，优先 AVC，避免 HEVC/AV1 混入导致部分播放器无法解码
        candidates = [v for v in dash.get('video') or [] if str(v.get('id')) == str(qn)]
        candidates.sort(key=lambda v: {7: 0, 12: 1, 13: 2}.get(v.get('codecid'), 3))
        video_xml = self._dash_media(candidates[0]) if candidates else ''

        audio_xml = ''
        for audio in dash.get('audio') or []:
            if self._audio_bitrate(audio.get('id')):
                audio_xml += self._dash_media(audio)

        return [200, 'application/dash+xml', self._dash_mpd(dash, video_xml, audio_xml)]

    def _dash_media(self, media):
        """单个 Representation 片段。"""
        media_id = media.get('id')
        codec_id = media.get('codecid') or 0
        codecs = media.get('codecs') or ''
        mime_type = media.get('mimeType') or ''
        segment = media.get('SegmentBase') or {}
        media_type = mime_type.split('/')[0] or 'video'

        if media_type == 'video':
            extra = (
                f"height='{media.get('height')}' width='{media.get('width')}' "
                f"frameRate='{media.get('frameRate')}' sar='{media.get('sar')}'"
            )
        else:
            extra = f"numChannels='2' sampleRate='{self._audio_bitrate(media_id) or 192000}'"

        # 主地址 + 备用地址都写进 BaseURL（DASH 客户端会按顺序尝试），
        # 规避 B 站偶发的 PCDN 边缘节点 403
        base_urls = [self._dash_base_url(self._raw_url(media))]
        for backup in media.get('backupUrl') or media.get('backup_url') or []:
            backup_url = self._dash_base_url(str(backup))
            if backup and backup_url not in base_urls:
                base_urls.append(backup_url)
        base_xml = ''.join(
            f'    <BaseURL>{url.replace("&", "&amp;")}</BaseURL>\n'
            for url in base_urls if url
        )

        return (
            '<AdaptationSet lang="chi">\n'
            f'  <ContentComponent contentType="{media_type}"/>\n'
            f'  <Representation id="{media_id}_{codec_id}" bandwidth="{media.get("bandwidth")}" '
            f'codecs="{codecs}" mimeType="{mime_type}" {extra} startWithSAP="{media.get("startWithSap")}">\n'
            f'{base_xml}'
            f'    <SegmentBase indexRange="{segment.get("indexRange")}">\n'
            f'      <Initialization range="{segment.get("Initialization")}"/>\n'
            '    </SegmentBase>\n'
            '  </Representation>\n'
            '</AdaptationSet>'
        )

    @staticmethod
    def _dash_mpd(dash, video_xml, audio_xml):
        duration = dash.get('duration') or 0
        min_buffer = dash.get('minBufferTime') or 1.5
        return (
            '<MPD xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns="urn:mpeg:dash:schema:mpd:2011" '
            'xsi:schemaLocation="urn:mpeg:dash:schema:mpd:2011 DASH-MPD.xsd" '
            f'type="static" mediaPresentationDuration="PT{duration}S" '
            f'minBufferTime="PT{min_buffer}S" '
            'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">\n'
            f'  <Period duration="PT{duration}S" start="PT0S">\n'
            f'{video_xml}\n'
            f'{audio_xml}\n'
            '  </Period>\n'
            '</MPD>'
        )
