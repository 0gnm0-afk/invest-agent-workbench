"""On-demand Telegram searches. Session, entities and results stay in memory."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
READ_OPS = {'status', 'dialogs', 'search'}
AUTH_OPS = {'login', 'verify', 'logout', 'save_credentials', 'clear_credentials'}
NEWS_TERMS = re.compile(r'속보|뉴스|기사|보도|공시|실적|발표|매출|가이던스|인수|합병|보고서|reuters|bloomberg|earnings|revenue|announc|breaking|news|guidance|filing', re.I)


def utc():
    return datetime.now(timezone.utc).isoformat()


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid_integer')
    return value


def fields(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError('invalid_fields')


def client_factory(api_id, api_hash):
    # Optional dependency isolated from the shared trading engine environment.
    sys.path.insert(0, str(ROOT / 'runtime/telegram-news-deps'))
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    logger = logging.getLogger('invest_telegram_news')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return TelegramClient(MemorySession(), api_id, api_hash,
                          receive_updates=False, catch_up=False, request_retries=2,
                          connection_retries=1, flood_sleep_threshold=0,
                          raise_last_call_error=True,
                          base_logger=logger, device_model='Investment news reader',
                          app_version='1.0')


def news_item(message, room):
    body = getattr(message, 'message', None) or ''
    if not isinstance(body, str) or not body.strip():
        return None
    urls = re.findall(r'https?://[^\s<>]+', body)
    urls += [e.url for e in (getattr(message, 'entities', None) or []) if getattr(e, 'url', None)]
    links = []
    for url in urls:
        url = url.rstrip('.,);]')
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                continue
            if parsed.hostname.lower() in ('t.me', 'telegram.me', 'www.t.me'):
                continue
        except ValueError:
            continue
        if url not in links:
            links.append(url[:2000])
    rule = 'external_link' if links else 'news_terms' if NEWS_TERMS.search(body) else None
    if not rule:
        return None
    username = room.get('username')
    link = f"https://t.me/{username}/{message.id}" if username else (
        f"https://t.me/c/{room['channel_id']}/{message.id}" if room.get('channel_id') else None)
    return dict(room_id=room['id'], room_title=room['title'], message_id=message.id,
                published_at=message.date.isoformat(), message_url=link,
                text=body[:4000], text_truncated=len(body)>4000,
                article_urls=links[:8], classification='news_candidate', matched_rule=rule)


class NewsService:
    def __init__(self, factory=client_factory, credentials=None):
        self.factory = factory
        self.credentials = credentials
        self.client = None
        self.state = 'not_connected'
        self.phone = self.code_hash = None
        self.rooms = {}
        self.room_rows = []
        self.rooms_loaded = False
        self.dialogs_truncated = False
        self.cooldown_until = 0
        self.last_error = None
        self.lock = asyncio.Lock()

    def status(self):
        return dict(state=self.state, storage='news_and_session_memory_only', news_persistence=False,
                    api_credentials_saved=bool(self.credentials and self.credentials.exists()),
                    session_persistence=False, receive_updates=False,
                    diagnostic=self.last_error,
                    retry_after_seconds=max(0, int(self.cooldown_until-time.monotonic())+1)
                    if self.cooldown_until>time.monotonic() else 0)

    async def request(self, op, args):
        async with self.lock:
            try:
                if op not in READ_OPS | AUTH_OPS:
                    raise ValueError('unsupported_operation')
                if op == 'status':
                    fields(args, []); return self.status()
                self.last_error = None
                if op not in ('logout','save_credentials','clear_credentials') and time.monotonic() < self.cooldown_until:
                    return {**self.status(), 'error':'telegram_rate_limited'}
                return await asyncio.wait_for(self.dispatch(op, args), timeout=45)
            except ValueError as exc:
                self.note_error(op,exc)
                return {**self.status(), 'error':str(exc) if str(exc) in {
                    'invalid_integer','invalid_fields','invalid_api_credentials','invalid_phone',
                    'login_already_started','login_not_started','invalid_code','invalid_password',
                    'authentication_required','load_rooms_first','unknown_room','invalid_query',
                    'invalid_dates','invalid_offset','unsupported_operation','api_credentials_missing',
                    'api_credentials_unreadable','api_credentials_save_failed','api_credentials_delete_failed'} else 'telegram_request_failed'}
            except Exception as exc:
                self.note_error(op,exc)
                name = type(exc).__name__
                if name == 'SessionPasswordNeededError':
                    self.state = 'password_required'; return self.status()
                if name in ('FloodWaitError', 'FloodPremiumWaitError'):
                    seconds = max(1, int(getattr(exc, 'seconds', 60)))
                    self.cooldown_until = time.monotonic()+seconds
                    return {**self.status(), 'error':'telegram_rate_limited'}
                codes = {'PhoneCodeInvalidError':'invalid_code', 'PhoneCodeExpiredError':'code_expired_restart_login',
                         'PasswordHashInvalidError':'invalid_password', 'ApiIdInvalidError':'invalid_api_credentials',
                         'PhoneNumberInvalidError':'invalid_phone', 'AuthKeyUnregisteredError':'session_expired',
                         'SessionRevokedError':'session_expired', 'ChannelPrivateError':'room_access_denied',
                         'ChatAdminRequiredError':'room_access_denied', 'ModuleNotFoundError':'dependency_missing',
                         'TimeoutError':'telegram_timeout'}
                if name in ('AuthKeyUnregisteredError', 'SessionRevokedError'):
                    self.state = 'session_expired'
                # Never forward exception bodies/tracebacks: they may include auth input.
                return {**self.status(), 'error':codes.get(name, 'telegram_request_failed')}

    def note_error(self, op, exc):
        # Safe diagnostics only. Never exception messages, locals or request bodies.
        tb=exc.__traceback__
        while tb and tb.tb_next:
            tb=tb.tb_next
        self.last_error=dict(operation=op,exception_type=type(exc).__name__,
                             module=Path(tb.tb_frame.f_code.co_filename).name if tb else None,
                             function=tb.tb_frame.f_code.co_name if tb else None,
                             line=tb.tb_lineno if tb else None)

    async def dispatch(self, op, args):
        if op == 'save_credentials':
            fields(args,['api_id','api_hash'])
            if not self.credentials:
                raise ValueError('api_credentials_save_failed')
            self.credentials.save(args.get('api_id'),args.get('api_hash'))
            return self.status()
        if op == 'clear_credentials':
            fields(args,[])
            if self.credentials:
                self.credentials.clear()
            return self.status()
        if op == 'logout':
            fields(args, [])
            client = self.client
            try:
                if client:
                    await client.log_out()
            finally:
                try:
                    if client:
                        await client.disconnect()
                finally:
                    self.client = None; self.phone = self.code_hash = None
                    self.rooms.clear(); self.room_rows.clear(); self.rooms_loaded = False
                    self.state = 'not_connected'
            return self.status()
        if op == 'login':
            fields(args, ['api_id','api_hash','phone'])
            if self.client:
                raise ValueError('login_already_started')
            supplied = args
            if 'api_id' not in args and 'api_hash' not in args:
                if not self.credentials:
                    raise ValueError('api_credentials_missing')
                supplied = self.credentials.load()
            api_id = integer(supplied.get('api_id'), 1, 2147483647)
            api_hash, phone = supplied.get('api_hash'), args.get('phone')
            if not isinstance(api_hash, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', api_hash):
                raise ValueError('invalid_api_credentials')
            if not isinstance(phone, str) or not re.fullmatch(r'\+[0-9]{7,15}', phone):
                raise ValueError('invalid_phone')
            self.client = self.factory(api_id, api_hash)
            try:
                await self.client.connect()
                sent = await self.client.send_code_request(phone)
                self.phone, self.code_hash = phone, sent.phone_code_hash
                self.state = 'code_required'
            except BaseException:
                client, self.client = self.client, None
                await client.disconnect()
                raise
            return self.status()
        if op == 'verify':
            fields(args, ['code','password'])
            if not self.client or self.state not in ('code_required','password_required'):
                raise ValueError('login_not_started')
            if self.state == 'code_required':
                code = args.get('code')
                if not isinstance(code, str) or not re.fullmatch(r'[0-9]{4,10}', code):
                    raise ValueError('invalid_code')
                await self.client.sign_in(phone=self.phone, code=code, phone_code_hash=self.code_hash)
            else:
                password = args.get('password')
                if not isinstance(password, str) or not 1 <= len(password) <= 256:
                    raise ValueError('invalid_password')
                await self.client.sign_in(password=password)
            self.phone = self.code_hash = None
            self.state = 'connected'
            return self.status()
        if self.state != 'connected':
            raise ValueError('authentication_required')
        if op == 'dialogs':
            fields(args, ['offset','limit','refresh'])
            offset = integer(args.get('offset',0),0,1000)
            limit = integer(args.get('limit',100),1,100)
            refresh = args.get('refresh',False)
            if type(refresh) is not bool or refresh and offset:
                raise ValueError('invalid_offset')
            if not self.rooms_loaded or refresh:
                dialogs = await self.client.get_dialogs(limit=1000, ignore_migrated=True)
                rooms, rows = {}, []
                for dialog in dialogs:
                    if not (dialog.is_group or dialog.is_channel):
                        continue
                    entity = dialog.entity
                    room = dict(id=str(dialog.id), title=str(dialog.title)[:200],
                                kind='channel' if getattr(entity,'broadcast',False) else 'group',
                                username=getattr(entity,'username',None),
                                channel_id=entity.id if dialog.is_channel else None)
                    rooms[room['id']] = (dialog.input_entity, room)
                    rows.append(room)
                self.rooms, self.room_rows = rooms, rows
                self.dialogs_truncated = len(dialogs) >= 1000
                self.rooms_loaded = True
            end = offset+limit
            return dict(state='connected', rooms=self.room_rows[offset:end], total=len(self.room_rows),
                        next_offset=end if end<len(self.room_rows) else None,
                        source_limit=1000, source_truncated=self.dialogs_truncated,
                        private_dialogs_excluded=True, storage='memory_only', fetched_at=utc())
        fields(args, ['room_id','query','start','end','offset_id','limit'])
        if not self.rooms_loaded:
            raise ValueError('load_rooms_first')
        room_id = args.get('room_id')
        if not isinstance(room_id, str) or room_id not in self.rooms:
            raise ValueError('unknown_room')
        query = args.get('query')
        if not isinstance(query, str) or not 1<=len(query.strip())<=200:
            raise ValueError('invalid_query')
        try:
            start, end = (datetime.strptime(args[k],'%Y-%m-%d').replace(tzinfo=KST) for k in ('start','end'))
            if not 0 <= (end-start).days <= 366:
                raise ValueError()
        except (ValueError,TypeError,KeyError):
            raise ValueError('invalid_dates') from None
        limit = integer(args.get('limit',30),1,100)
        offset = integer(args.get('offset_id',0),0,2147483647)
        peer, room = self.rooms[room_id]
        # Exact server-side search; no full-history download, media or read receipts.
        from telethon.tl.functions.messages import SearchRequest
        from telethon.tl.types import InputMessagesFilterEmpty
        upper = end+timedelta(days=1)
        result = await self.client(SearchRequest(peer=peer,q=query.strip(),filter=InputMessagesFilterEmpty(),
            min_date=start-timedelta(seconds=1),max_date=upper,offset_id=offset,
            add_offset=0,limit=limit,max_id=0,min_id=0,hash=0))
        messages = result.messages
        items=[]
        for message in messages:
            stamp = getattr(message,'date',None)
            if stamp is not None and start <= stamp < upper:
                item = news_item(message, room)
                if item: items.append(item)
        return dict(state='connected', query=query.strip(), room_id=room_id,
                    start=args['start'],end=args['end'],timezone='Asia/Seoul',
                    items=items,scanned=len(messages),news_candidates=len(items),
                    next_offset_id=messages[-1].id if len(messages)==limit and messages else None,
                    server_match_count=getattr(result,'count',None), fetched_at=utc(),
                    storage='not_saved', classification_note='링크 또는 뉴스 관련 문구로 고른 후보입니다. 실제 기사 여부와 사실은 원 출처에서 대조하세요.')
