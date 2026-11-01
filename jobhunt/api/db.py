"""Async PostgreSQL persistence for Neon, using SQLAlchemy AsyncSession."""
from __future__ import annotations
import hashlib, json, os, socket, uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.exc import IntegrityError
from ..fetch import Job
from sqlalchemy.engine import make_url


def prefer_ipv4() -> None:
    """Resolve IPv4 addresses before IPv6 (same addresses, different order).

    On networks where IPv6 is advertised but blackholed, asyncpg spends its whole
    connect timeout on the first AAAA address and never reaches a working IPv4 one.
    """
    if getattr(socket.getaddrinfo, "_ipv4_first", False):
        return
    real = socket.getaddrinfo

    def getaddrinfo(*args, **kwargs):
        infos = real(*args, **kwargs)
        return sorted(infos, key=lambda i: i[0] == socket.AF_INET6)

    getaddrinfo._ipv4_first = True  # type: ignore[attr-defined]
    socket.getaddrinfo = getaddrinfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS profile (
    user_id     BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data        TEXT NOT NULL,
    source_file TEXT,
    updated_at  TEXT NOT NULL,
    manual_fields TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS resumes (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    data        BYTEA NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resumes_user ON resumes (user_id, id DESC);
CREATE TABLE IF NOT EXISTS study_plans (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    plan       TEXT
);
CREATE INDEX IF NOT EXISTS idx_study_plans_user ON study_plans (user_id, id DESC);
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,          -- running | succeeded | failed
    stage       TEXT,
    message     TEXT,
    options     TEXT,
    counts      TEXT,
    warnings    TEXT,
    error       TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    ats         TEXT,
    company     TEXT,
    title       TEXT,
    location    TEXT,
    url         TEXT,
    description TEXT,
    posted_at   TEXT,
    salary      TEXT,
    run_id      TEXT,
    first_seen  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs (first_seen DESC, job_id);
CREATE TABLE IF NOT EXISTS user_jobs (
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id     TEXT NOT NULL,
    company    TEXT,
    title      TEXT,
    location   TEXT,
    url        TEXT,
    posted_at  TEXT,
    salary     TEXT,
    applied_on TEXT NOT NULL,
    PRIMARY KEY (user_id, job_id)
);
CREATE TABLE IF NOT EXISTS seen_jobs (
    job_id     TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL
);
"""


class RunBlocked(Exception):
    def __init__(self, kind: str, run_id: str | None = None, retry_after: int = 0):
        super().__init__(kind); self.kind, self.run_id, self.retry_after = kind, run_id, retry_after

def _fmt(dt: datetime) -> str: return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
def _now() -> str: return _fmt(datetime.now(timezone.utc))

def _params(sql: str, params: Any = None):
    """Translate legacy positional %s placeholders to SQLAlchemy named binds."""
    if params is None: return sql, {}
    if isinstance(params, dict): return sql, params
    values = list(params) if isinstance(params, (tuple, list)) else [params]
    parts = sql.split('%s')
    if len(parts)-1 != len(values):
        # Queries using PostgreSQL's literal percent syntax are not expected here.
        raise ValueError(f"SQL bind mismatch: expected {len(parts)-1}, got {len(values)}")
    out = parts[0]; bound = {}
    for i, value in enumerate(values): out += f":p{i}" + parts[i+1]; bound[f"p{i}"] = value
    
    return out, bound

def normalize_database_url(database_url: str) -> str:
    parsed = make_url(database_url)

    if parsed.drivername in ("postgres", "postgresql", "postgresql+psycopg"):
        parsed = parsed.set(drivername="postgresql+asyncpg")

    query = dict(parsed.query)
    query.pop("channel_binding", None)

    sslmode = query.pop("sslmode", None)
    if sslmode and "ssl" not in query:
        query["ssl"] = sslmode

    return parsed.set(query=query).render_as_string(hide_password=False)
    return url.set(query=query).render_as_string(hide_password=False)

class Database:
    """One AsyncSession per unit of work; transactions are explicit and short-lived."""
    def __init__(self, database_url: str | None = None):
        if os.getenv("DB_PREFER_IPV4", "1") != "0":
            prefer_ipv4()
        url = normalize_database_url(database_url or os.getenv('DATABASE_URL', ''))
        if not url: raise RuntimeError('DATABASE_URL must be set to connect to Neon PostgreSQL')
        url = url.replace('postgres://', 'postgresql+asyncpg://', 1).replace('postgresql://', 'postgresql+asyncpg://', 1)
        url = url.replace('postgresql+psycopg://', 'postgresql+asyncpg://', 1)
        # asyncpg's prepared-statement cache can conflict with PgBouncer-style
        # transaction pooling on some hosted PostgreSQL endpoints.
        if '-pooler.' in url and 'prepared_statement_cache_size=' not in url:
            url += ('&' if '?' in url else '?') + 'prepared_statement_cache_size=0'
        self.engine = create_async_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2, pool_recycle=1800, connect_args={"timeout": float(os.getenv("DB_CONNECT_TIMEOUT", "30")), "command_timeout": 60})
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def _one(self, sql, params=None):
        async with self.sessions() as s:
            q, b = _params(sql, params); r = await s.execute(text(q), b)
            row = r.mappings().first(); return dict(row) if row else None
    async def _all(self, sql, params=None):
        async with self.sessions() as s:
            q, b = _params(sql, params); r = await s.execute(text(q), b)
            return [dict(x) for x in r.mappings().all()]
    async def _write(self, sql, params=None):
        async with self.sessions.begin() as s:
            q,b = _params(sql,params); r=await s.execute(text(q),b); return r.rowcount

    async def init(self):
        async with self.sessions.begin() as s:
            legacy = (await s.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name='jobs' AND column_name='user_id'"))).first() is not None
            if legacy:
                await s.execute(text('ALTER TABLE jobs RENAME TO jobs_legacy'))
                await s.execute(text('ALTER INDEX IF EXISTS jobs_pkey RENAME TO jobs_legacy_pkey'))
            for statement in SCHEMA.split(';'):
                if statement.strip(): await s.execute(text(statement))
            await s.execute(text('ALTER TABLE study_plans ADD COLUMN IF NOT EXISTS plan TEXT'))
            await s.execute(text("ALTER TABLE profile ADD COLUMN IF NOT EXISTS manual_fields TEXT NOT NULL DEFAULT '[]'"))
            await s.execute(text('ALTER TABLE runs DROP COLUMN IF EXISTS user_id'))
            # An applied job outlives the catalog row (jobs expire), so user_jobs keeps its own copy.
            await s.execute(text('ALTER TABLE user_jobs DROP CONSTRAINT IF EXISTS user_jobs_job_id_fkey'))
            for col in ('company', 'title', 'location', 'url', 'posted_at', 'salary'):
                await s.execute(text(f'ALTER TABLE user_jobs ADD COLUMN IF NOT EXISTS {col} TEXT'))
            await s.execute(text(
                'UPDATE user_jobs uj SET company=j.company,title=j.title,location=j.location,url=j.url,'
                'posted_at=j.posted_at,salary=j.salary FROM jobs j WHERE j.job_id=uj.job_id AND uj.title IS NULL'))
            if legacy:
                # Per-user copies collapse into one global row per job; only the
                # applied marks stay attached to the user.
                await s.execute(text(
                    'INSERT INTO jobs(job_id,ats,company,title,location,url,description,posted_at,salary,run_id,first_seen) '
                    'SELECT DISTINCT ON (job_id) job_id,ats,company,title,location,url,description,posted_at,salary,run_id,first_seen '
                    'FROM jobs_legacy ORDER BY job_id,first_seen'))
                await s.execute(text(
                    "INSERT INTO user_jobs(user_id,job_id,company,title,location,url,posted_at,salary,applied_on) "
                    "SELECT user_id,job_id,company,title,location,url,posted_at,salary,COALESCE(applied_on,first_seen) "
                    "FROM jobs_legacy WHERE status='applied' ON CONFLICT DO NOTHING"))
                await s.execute(text('DROP TABLE jobs_legacy'))
            await s.execute(text('INSERT INTO seen_jobs(job_id,first_seen) SELECT job_id,first_seen FROM jobs ON CONFLICT DO NOTHING'))

    @staticmethod
    def _user(row):
        if not row:
            return None

        fields = (
            'id', 'email', 'created_at',
            'password_hash', 'failed_attempts', 'locked_until'
        )

        user = {k: row[k] for k in fields}

        if 'has_profile' in row:
            user['has_profile'] = row['has_profile']

        return user
    async def create_user(self,username,email,password_hash):
        try:
            async with self.sessions.begin() as session:
                result=await session.execute(
                    text("""
                        INSERT INTO users
                        (username,email,password_hash,created_at)
                        VALUES (:u,:e,:p,:c)
                        RETURNING id, username, email, created_at
                    """), 
                    {
                        "u": username,
                        "e":email,
                        "p":password_hash,
                        "c":_now()
                    }
                )
                
                return result.mappings().one()

        except IntegrityError: 
            return None
        
    async def get_user(self,user_id): return self._user(await self._one('SELECT * FROM users WHERE id=%s',(user_id,)))
    async def get_user_by_email(self, email):
        row = await self._one(
            """
            SELECT u.*,
                EXISTS (
                    SELECT 1
                    FROM profile p
                    WHERE p.user_id = u.id
                ) AS has_profile
            FROM users u
            WHERE u.email = %s
            """,
            (email,)
        )
        return self._user(row)
    async def lock_remaining(self,user):
        until=user.get('locked_until')
        if not until:return 0
        left=(datetime.fromisoformat(until)-datetime.now(timezone.utc)).total_seconds()
        if left>0:return int(left)+1
        await self.reset_failed_logins(user['id']); return 0
    async def record_failed_login(self,user_id,max_attempts,lockout_s):
        await self._write('UPDATE users SET failed_attempts=failed_attempts+1, locked_until=CASE WHEN failed_attempts+1 >= %s THEN %s ELSE locked_until END WHERE id=%s',(max_attempts,_fmt(datetime.now(timezone.utc)+timedelta(seconds=lockout_s)),user_id))
    async def reset_failed_logins(self,user_id): await self._write('UPDATE users SET failed_attempts=0,locked_until=NULL WHERE id=%s',(user_id,))
    async def create_session(self,user_id):
        sid,now=uuid.uuid4().hex,_now()
        async with self.sessions.begin() as s:
            await s.execute(text('INSERT INTO sessions(id,user_id,created_at,last_activity) VALUES (:i,:u,:n,:n)'),{'i':sid,'u':user_id,'n':now})
            await s.execute(text('DELETE FROM sessions WHERE last_activity < :cut'),{'cut':_fmt(datetime.now(timezone.utc)-timedelta(days=7))})
        return sid
    async def touch_session(self,sid,user_id,idle_seconds):
        async with self.sessions.begin() as s:
            r=await s.execute(text('UPDATE sessions SET last_activity=:n WHERE id=:i AND user_id=:u AND revoked=0 AND last_activity >= :cut'),{'n':_now(),'i':sid,'u':user_id,'cut':_fmt(datetime.now(timezone.utc)-timedelta(seconds=idle_seconds))})
            if not r.rowcount:return None
            row=(await s.execute(text('SELECT * FROM users WHERE id=:u'),{'u':user_id})).mappings().first()
            return self._user(dict(row) if row else None)
    async def revoke_session(self,sid): await self._write('UPDATE sessions SET revoked=1 WHERE id=%s',(sid,))
    async def save_profile(
        self, user_id, profile, source_file, resume=None, *,
        mark_manual_fields=None, clear_manual_fields=False
    ):
        """Persist extracted profile data while tracking fields manually corrected by users.

        New resume uploads preserve manually edited fields. Explicit re-extraction can
        clear that protection by passing clear_manual_fields=True.
        """
        async with self.sessions.begin() as s:
            existing_result = await s.execute(
                text("SELECT data, manual_fields FROM profile WHERE user_id=:u"),
                {"u": user_id},
            )
            existing = existing_result.mappings().first()
            manual_fields = set()
            if existing:
                try:
                    manual_fields = set(json.loads(existing["manual_fields"] or "[]"))
                except (TypeError, ValueError):
                    manual_fields = set()

            if clear_manual_fields:
                manual_fields.clear()
            elif existing and manual_fields:
                previous = json.loads(existing["data"])
                for field in manual_fields:
                    if field in previous:
                        profile[field] = previous[field]

            if mark_manual_fields:
                manual_fields.update(mark_manual_fields)

            await s.execute(
                text("""
                    INSERT INTO profile(user_id,data,source_file,updated_at,manual_fields)
                    VALUES (:u,:d,:f,:n,:m)
                    ON CONFLICT(user_id) DO UPDATE SET
                        data=excluded.data,
                        source_file=COALESCE(excluded.source_file,profile.source_file),
                        updated_at=excluded.updated_at,
                        manual_fields=excluded.manual_fields
                """),
                {
                    "u": user_id,
                    "d": json.dumps(profile, ensure_ascii=False),
                    "f": source_file,
                    "n": _now(),
                    "m": json.dumps(sorted(manual_fields)),
                },
            )
            if resume:
                data = resume["data"]
                digest = hashlib.sha256(data).hexdigest()
                await s.execute(
                    text("DELETE FROM resumes WHERE user_id=:u AND sha256=:h"),
                    {"u": user_id, "h": digest},
                )
                await s.execute(
                    text("""
                        INSERT INTO resumes(user_id,filename,media_type,size,sha256,data,uploaded_at)
                        VALUES (:u,:f,:m,:z,:h,:d,:n)
                    """),
                    {
                        "u": user_id, "f": resume["filename"],
                        "m": resume["media_type"], "z": len(data),
                        "h": digest, "d": data, "n": _now(),
                    },
                )
        return await self.get_profile(user_id)

    async def get_profile(self, user_id):
        row = await self._one(
            "SELECT data,source_file,updated_at FROM profile WHERE user_id=%s",
            (user_id,),
        )
        if not row:
            return None
        data = row["data"]
        profile = json.loads(data) if isinstance(data, str) else data
        return {
            "profile": profile,
            "source_file": row["source_file"],
            "updated_at": row["updated_at"],
            "resume": await self.resume_info(user_id),
        }

    async def save_study_plan(self,user_id,data):
        created=_now()
        async with self.sessions.begin() as s:
            r=await s.execute(text('INSERT INTO study_plans(user_id,data,created_at) VALUES (:u,:d,:c) RETURNING id'),{'u':user_id,'d':json.dumps(data,ensure_ascii=False),'c':created}); pid=r.scalar_one()
        return {**data,'id':pid,'created_at':created,'plan':None}
    async def set_study_plan_text(self,user_id,plan_id,plan): return await self._write('UPDATE study_plans SET plan=%s WHERE id=%s AND user_id=%s',(plan,plan_id,user_id))
    async def get_study_plan(self,user_id,plan_id):
        r=await self._one('SELECT * FROM study_plans WHERE user_id=%s AND id=%s',(user_id,plan_id))
        return {**json.loads(r['data']),'id':r['id'],'created_at':r['created_at'],'plan':r['plan']} if r else None
    async def latest_study_plan(self,user_id):
        r=await self._one('SELECT * FROM study_plans WHERE user_id=%s ORDER BY id DESC LIMIT 1',(user_id,))
        return {**json.loads(r['data']),'id':r['id'],'created_at':r['created_at'],'plan':r['plan']} if r else None
    _INFO_COLS='id,filename,media_type,size,sha256,uploaded_at'
    async def resume_info(self,u): return await self._one(f'SELECT {self._INFO_COLS} FROM resumes WHERE user_id=%s ORDER BY id DESC LIMIT 1',(u,))
    async def list_resumes(self,u): return await self._all(f'SELECT {self._INFO_COLS} FROM resumes WHERE user_id=%s ORDER BY id DESC',(u,))
    async def count_resumes(self,u): return (await self._one('SELECT COUNT(*) AS count FROM resumes WHERE user_id=%s',(u,)))['count']
    async def has_resume_hash(self,u,h): return bool(await self._one('SELECT 1 AS found FROM resumes WHERE user_id=%s AND sha256=%s',(u,h)))
    async def get_resume(self,u,rid=None):
        if rid is None:return await self._one('SELECT * FROM resumes WHERE user_id=%s ORDER BY id DESC LIMIT 1',(u,))
        return await self._one('SELECT * FROM resumes WHERE user_id=%s AND id=%s',(u,rid))
    async def delete_resume(self,u,rid): return bool(await self._write('DELETE FROM resumes WHERE user_id=%s AND id=%s',(u,rid)))
    STALE_RUN_AFTER = timedelta(hours=2)
    async def start_run(self,options,min_interval_s):
        """One scrape at a time for the whole service, and at most one per interval."""
        async with self.sessions.begin() as s:
            await s.execute(text('SELECT pg_advisory_xact_lock(721904231)'))
            # A run that never finished (crash, restart) must not block scraping forever.
            now=datetime.now(timezone.utc)
            await s.execute(text("UPDATE runs SET status='failed',stage='failed',error='run did not finish',finished_at=:n WHERE status='running' AND started_at<:c"),{'n':_fmt(now),'c':_fmt(now-self.STALE_RUN_AFTER)})
            running=(await s.execute(text("SELECT id FROM runs WHERE status='running'"))).mappings().first()
            if running: raise RunBlocked('running',running['id'])
            last=(await s.execute(text("SELECT started_at FROM runs WHERE status='succeeded' ORDER BY started_at DESC LIMIT 1"))).mappings().first()
            if last and min_interval_s>0:
                wait=(datetime.fromisoformat(last['started_at'])+timedelta(seconds=min_interval_s)-now).total_seconds()
                if wait>0: raise RunBlocked('rate_limited',retry_after=int(wait)+1)
            rid=uuid.uuid4().hex
            await s.execute(text("INSERT INTO runs(id,status,stage,message,options,started_at) VALUES (:i,'running','starting','',:o,:n)"),{'i':rid,'o':json.dumps(options),'n':_now()})
        return await self.get_run(rid)
    async def update_run(self,rid,stage,message): await self._write('UPDATE runs SET stage=%s,message=%s WHERE id=%s',(stage,message,rid))
    async def finish_run(self,rid,*,error=None,counts=None,warnings=None):
        await self._write("UPDATE runs SET status=%s,stage=%s,message='',error=%s,counts=%s,warnings=%s,finished_at=%s WHERE id=%s",('failed' if error else 'succeeded','failed' if error else 'done',error,json.dumps(counts) if counts is not None else None,json.dumps(warnings or []),_now(),rid))
    @staticmethod
    def _run(r): return {'run_id':r['id'],'status':r['status'],'stage':r['stage'],'message':r['message'] or '', 'error':r['error'],'counts':json.loads(r['counts']) if r['counts'] else None,'warnings':json.loads(r['warnings']) if r['warnings'] else [],'started_at':r['started_at'],'finished_at':r['finished_at']}
    async def get_run(self,rid):
        r=await self._one('SELECT * FROM runs WHERE id=%s',(rid,)); return self._run(r) if r else None
    async def latest_run(self):
        r=await self._one('SELECT * FROM runs ORDER BY started_at DESC LIMIT 1'); return self._run(r) if r else None
    async def seen_ids(self):
        """Every job id scraped before, including expired ones, so they are not re-added."""
        return {r['job_id'] for r in await self._all('SELECT job_id FROM seen_jobs')}
    async def save_jobs(self,jobs,rid):
        """Store postings not seen before; a job already in the catalog is left untouched."""
        if not jobs:return 0
        async with self.sessions.begin() as s:
            for j in jobs:
                now=_now()
                await s.execute(text('INSERT INTO jobs(job_id,ats,company,title,location,url,description,posted_at,salary,run_id,first_seen) VALUES (:id,:ats,:co,:ti,:lo,:url,:de,:po,:sa,:run,:n) ON CONFLICT(job_id) DO NOTHING'),{'id':j.job_id,'ats':j.ats,'co':j.company,'ti':j.title,'lo':j.location,'url':j.url,'de':j.description,'po':j.posted_at,'sa':j.salary,'run':rid,'n':now})
                await s.execute(text('INSERT INTO seen_jobs(job_id,first_seen) VALUES (:id,:n) ON CONFLICT(job_id) DO NOTHING'),{'id':j.job_id,'n':now})
        return len(jobs)
    @staticmethod
    def retention_hours():
        try: return float(os.getenv('JOB_RETENTION_HOURS','24'))
        except ValueError: return 24.0
    def _since(self,hours=None):
        """Oldest first_seen still shown: 24h by default, never longer than the retention."""
        return _fmt(datetime.now(timezone.utc)-timedelta(hours=min(hours or 24,self.retention_hours())))
    async def purge_expired(self):
        """Delete jobs older than the retention window. Applied jobs live on in user_jobs."""
        cutoff=_fmt(datetime.now(timezone.utc)-timedelta(hours=self.retention_hours()))
        removed=await self._write('DELETE FROM jobs WHERE first_seen<%s',(cutoff,))
        # The id list is kept much longer than the jobs so an expired posting is not re-added.
        await self._write('DELETE FROM seen_jobs WHERE first_seen<%s',(_fmt(datetime.now(timezone.utc)-timedelta(days=30)),))
        return removed
    _JOB_COLS='j.job_id,j.company,j.title,j.location,j.url,j.posted_at,j.salary,j.first_seen'
    @staticmethod
    def _job(r,detail=False):
        out={k:r[k] for k in ('job_id','company','title','location','url','posted_at','salary','first_seen')}
        if detail:out['description']=r['description'] or ''
        return out
    def _filters(self,company,q,location,hours=None):
        w=['j.first_seen>=:since']; b={'since':self._since(hours)}
        if company:w.append('LOWER(j.company)=LOWER(:company)'); b['company']=company
        if q:w.append('(LOWER(j.title) LIKE :q OR LOWER(j.company) LIKE :q)'); b['q']=f'%{q.lower()}%'
        if location:w.append('LOWER(j.location) LIKE :loc'); b['loc']=f'%{location.lower()}%'
        return ' AND '.join(w),b
    async def list_jobs(self,*,company=None,q=None,location=None,hours=None,limit=20,offset=0):
        clause,b=self._filters(company,q,location,hours)
        total=(await self._one(f'SELECT COUNT(*) AS count FROM jobs j WHERE {clause}',b))['count']
        rows=await self._all(f'SELECT {self._JOB_COLS} FROM jobs j WHERE {clause} ORDER BY j.first_seen DESC,j.job_id LIMIT :lim OFFSET :off',{**b,'lim':limit,'off':offset})
        return total,[self._job(r) for r in rows]
    async def list_applied(self,u,*,limit=20,offset=0):
        total=(await self._one('SELECT COUNT(*) AS count FROM user_jobs WHERE user_id=%s',(u,)))['count']
        rows=await self._all('SELECT job_id,company,title,location,url,posted_at,salary,applied_on FROM user_jobs WHERE user_id=:u ORDER BY applied_on DESC,job_id LIMIT :lim OFFSET :off',{'u':u,'lim':limit,'off':offset})
        return total,rows
    async def jobs_for_matching(self,u,*,company=None,q=None,location=None,status=None,hours=None,cap=2000):
        clause,b=self._filters(company,q,location,hours)
        if status=='applied':clause+=' AND uj.applied_on IS NOT NULL'
        elif status=='new':clause+=' AND uj.applied_on IS NULL'
        rows=await self._all(f"SELECT {self._JOB_COLS},j.description,CASE WHEN uj.applied_on IS NULL THEN 'new' ELSE 'applied' END AS status FROM jobs j LEFT JOIN user_jobs uj ON uj.job_id=j.job_id AND uj.user_id=:u WHERE {clause} ORDER BY j.first_seen DESC,j.job_id LIMIT :cap",{**b,'u':u,'cap':cap})
        return [{**r,'description':r['description'] or ''} for r in rows]
    async def postings_for_insights(self,*,company=None,q=None,location=None,cap=2000):
        clause,b=self._filters(company,q,location)
        rows=await self._all(f'SELECT j.title,j.description FROM jobs j WHERE {clause} ORDER BY j.first_seen DESC,j.job_id LIMIT :cap',{**b,'cap':cap})
        return [{'title':r['title'] or '','description':r['description'] or ''} for r in rows]
    async def get_job(self,jid):
        r=await self._one('SELECT * FROM jobs WHERE job_id=%s AND first_seen>=%s',(jid,self._since())); return self._job(r,True) if r else None
    async def mark_applied(self,u,jid):
        """Record that the user applied (the first date is kept). Returns applied_on, or None if no such job."""
        job=await self.get_job(jid)
        if not job: return None
        await self._write('INSERT INTO user_jobs(user_id,job_id,company,title,location,url,posted_at,salary,applied_on) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,job_id) DO NOTHING',(u,jid,job['company'],job['title'],job['location'],job['url'],job['posted_at'],job['salary'],_now()))
        return (await self._one('SELECT applied_on FROM user_jobs WHERE user_id=%s AND job_id=%s',(u,jid)))['applied_on']
    async def stats(self,u=None):
        tracked=(await self._one('SELECT COUNT(*) AS n FROM jobs WHERE first_seen>=%s',(self._since(),)))['n']
        applied=None if u is None else (await self._one('SELECT COUNT(*) AS n FROM user_jobs WHERE user_id=%s',(u,)))['n']
        return {'tracked':tracked,'applied':applied}
    async def close(self): await self.engine.dispose()
