import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from . import fixturevideo, jobs, odds, settle, sync
from .config import settings
from .db import WORLD_CODE, get_session, init_db
from .models import Bet, Fixture, League, LeagueMember, Team, User
from .schemas import (BetIn, EloIn, LeagueCreateIn, LeagueJoinIn, ProfileIn,
                      RegisterIn, ResultIn, TokenOut)
from .security import (create_access_token, get_current_user, hash_password,
                       require_admin, verify_password)

SCORELINE_RE = re.compile(r"^\d+-\d+$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    sync.load_fixtures_from_json()   # local cache only, no network
    jobs.start_scheduler()
    yield
    jobs.stop_scheduler()


app = FastAPI(title="World Cup 2026 Betting Pool", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------- helpers -----------------------------

def _elo(session: Session, team_id: int) -> float:
    team = session.get(Team, team_id)
    return team.elo if team else 1700.0


def _logo(session: Session, team_id: int):
    team = session.get(Team, team_id)
    return team.logo if team else None


def _window_open(fx: Fixture) -> bool:
    if fx.status != "NS":
        return False
    now = datetime.utcnow()
    open_at = fx.kickoff - timedelta(hours=settings.bet_open_hours)
    close_at = fx.kickoff - timedelta(minutes=settings.bet_close_minutes)
    return open_at <= now <= close_at


def _fixture_dict(session: Session, fx: Fixture, scorelines: bool = False) -> dict:
    home_elo, away_elo = _elo(session, fx.home_id), _elo(session, fx.away_id)
    o = odds.compute_odds(home_elo, away_elo, neutral=True)
    data = {
        "id": fx.id,
        "home": {"id": fx.home_id, "name": fx.home_name, "logo": _logo(session, fx.home_id)},
        "away": {"id": fx.away_id, "name": fx.away_name, "logo": _logo(session, fx.away_id)},
        "kickoff": fx.kickoff.isoformat() + "Z",
        "venue": fx.venue,
        "city": fx.city,
        "country": fx.country,
        "stage": fx.stage,
        "status": fx.status,
        "home_score": fx.home_score,
        "away_score": fx.away_score,
        "settled": fx.settled,
        "betting_open": _window_open(fx),
        "closes_at": (fx.kickoff - timedelta(minutes=settings.bet_close_minutes)).isoformat() + "Z",
        "odds": o["result"],
        "expected_goals": o["expected_goals"],
    }
    if scorelines:
        data["scorelines"] = o["scorelines"]
    return data


# ----------------------------- auth -----------------------------

@app.post("/api/auth/register", response_model=TokenOut)
def register(body: RegisterIn, session: Session = Depends(get_session)):
    if session.exec(select(User).where(User.username == body.username)).first():
        raise HTTPException(status_code=400, detail="Username already taken")
    # The very first person to register becomes the admin (can import fixtures, tweak Elo).
    is_first_user = session.exec(select(User)).first() is None
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        supported_country=body.supported_country,
        mascot=body.mascot,
        is_admin=is_first_user,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    # Everyone joins the global World league on signup.
    world = session.exec(select(League).where(League.code == WORLD_CODE)).first()
    if world is None:
        world = League(name="World", code=WORLD_CODE, is_world=True, created_by=user.id)
        session.add(world)
        session.commit()
        session.refresh(world)
    session.add(LeagueMember(league_id=world.id, user_id=user.id))
    session.commit()
    return TokenOut(access_token=create_access_token(user.username))


@app.post("/api/auth/login", response_model=TokenOut)
def login(form: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.username == form.username)).first()
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    return TokenOut(access_token=create_access_token(user.username))


@app.get("/api/me")
def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id, "username": user.username, "display_name": user.display_name,
        "supported_country": user.supported_country, "avatar_path": user.avatar_path,
        "mascot": user.mascot, "is_admin": user.is_admin,
    }


@app.patch("/api/me")
def update_me(body: ProfileIn, session: Session = Depends(get_session),
              user: User = Depends(get_current_user)):
    if body.mascot is not None:
        user.mascot = body.mascot
    if body.supported_country is not None:
        user.supported_country = body.supported_country
    session.add(user)
    session.commit()
    session.refresh(user)
    return {
        "id": user.id, "username": user.username, "display_name": user.display_name,
        "supported_country": user.supported_country, "avatar_path": user.avatar_path,
        "mascot": user.mascot, "is_admin": user.is_admin,
    }


# ----------------------------- fixtures + odds -----------------------------

@app.get("/api/fixtures")
def list_fixtures(scope: str = "upcoming", session: Session = Depends(get_session),
                  user: User = Depends(get_current_user)):
    rows = session.exec(select(Fixture).order_by(Fixture.kickoff)).all()
    if scope == "open":
        rows = [f for f in rows if _window_open(f)]
    elif scope == "upcoming":
        rows = [f for f in rows if f.status == "NS"]
    elif scope == "live":
        rows = [f for f in rows if f.status == "LIVE"]
    elif scope == "finished":
        rows = [f for f in rows if f.settled]
    # scope == "all" -> everything
    return [_fixture_dict(session, f) for f in rows]


@app.get("/api/fixtures/{fixture_id}")
def fixture_detail(fixture_id: int, session: Session = Depends(get_session),
                   user: User = Depends(get_current_user)):
    fx = session.get(Fixture, fixture_id)
    if not fx:
        raise HTTPException(status_code=404, detail="Fixture not found")
    data = _fixture_dict(session, fx, scorelines=True)
    mine = session.exec(
        select(Bet).where(
            Bet.user_id == user.id, Bet.fixture_id == fx.id, Bet.status == "pending"
        ).order_by(Bet.placed_at)
    ).first()
    data["my_bet"] = (
        {"market": mine.market, "selection": mine.selection,
         "stake": mine.stake, "odds": mine.odds, "payout": mine.payout}
        if mine else None
    )
    return data


@app.get("/api/fixtures/{fixture_id}/video")
def fixture_video(fixture_id: int, session: Session = Depends(get_session),
                  user: User = Depends(get_current_user)):
    """URL of the pre-rendered flyover for this fixture. If betting has already opened
    but the video wasn't pre-rendered yet, render it on demand so it can play now."""
    name = fixturevideo.find_video_file(fixture_id)
    if name:
        return {"url": f"/media/fixtures/{name}", "ready": True}
    fx = session.get(Fixture, fixture_id)
    if fx and fixturevideo.window_started(fx):
        name = fixturevideo.render_fixture(fx)   # serialized + dedup'd; blocks ~a few seconds
        if name:
            return {"url": f"/media/fixtures/{name}", "ready": True}
    return {"url": None, "ready": False}


# ----------------------------- betting -----------------------------

@app.post("/api/bets")
def place_bet(body: BetIn, session: Session = Depends(get_session),
              user: User = Depends(get_current_user)):
    fx = session.get(Fixture, body.fixture_id)
    if not fx:
        raise HTTPException(status_code=404, detail="Fixture not found")

    if fx.status != "NS":
        raise HTTPException(status_code=400, detail="Match already started or finished")
    now = datetime.utcnow()
    open_at = fx.kickoff - timedelta(hours=settings.bet_open_hours)
    close_at = fx.kickoff - timedelta(minutes=settings.bet_close_minutes)
    if now < open_at:
        raise HTTPException(status_code=400, detail=f"Betting opens at {open_at.isoformat()}Z")
    if now > close_at:
        raise HTTPException(status_code=400, detail="Betting is closed for this match")

    if not (settings.min_stake <= body.stake <= settings.max_stake):
        raise HTTPException(status_code=400,
                            detail=f"Stake must be {settings.min_stake}-{settings.max_stake}")

    market = body.market.upper()
    selection = body.selection.upper() if market == "1X2" else body.selection
    if market == "1X2":
        if selection not in ("HOME", "DRAW", "AWAY"):
            raise HTTPException(status_code=400, detail="selection must be HOME, DRAW or AWAY")
    elif market == "SCORELINE":
        if not SCORELINE_RE.match(selection):
            raise HTTPException(status_code=400, detail="scoreline must look like '2-1'")
    else:
        raise HTTPException(status_code=400, detail="market must be 1X2 or SCORELINE")

    home_elo, away_elo = _elo(session, fx.home_id), _elo(session, fx.away_id)
    price = odds.odds_for_selection(home_elo, away_elo, market, selection, neutral=True)
    if price is None:
        raise HTTPException(status_code=400, detail="No odds available for that selection")

    # One bet per user per fixture: replace the existing pending bet (across either
    # market) instead of stacking a new one. Any duplicates from earlier are collapsed.
    existing = session.exec(
        select(Bet).where(
            Bet.user_id == user.id,
            Bet.fixture_id == fx.id,
            Bet.status == "pending",
        ).order_by(Bet.placed_at)
    ).all()

    if existing:
        bet = existing[0]
        for extra in existing[1:]:
            session.delete(extra)
        bet.market = market
        bet.selection = selection
        bet.stake = body.stake
        bet.odds = price
        bet.payout = round(body.stake * price, 2)
        bet.placed_at = datetime.utcnow()
        replaced = True
    else:
        bet = Bet(
            user_id=user.id, fixture_id=fx.id, market=market, selection=selection,
            stake=body.stake, odds=price, payout=round(body.stake * price, 2),
        )
        replaced = False
    session.add(bet)
    session.commit()
    session.refresh(bet)
    return {
        "id": bet.id, "fixture_id": bet.fixture_id, "market": bet.market,
        "selection": bet.selection, "stake": bet.stake, "odds": bet.odds,
        "potential_payout": bet.payout, "potential_profit": round(bet.payout - bet.stake, 2),
        "status": bet.status, "replaced": replaced,
    }


@app.get("/api/bets/me")
def my_bets(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    bets = session.exec(
        select(Bet).where(Bet.user_id == user.id).order_by(Bet.placed_at.desc())
    ).all()
    out = []
    for b in bets:
        fx = session.get(Fixture, b.fixture_id)
        out.append({
            "id": b.id, "market": b.market, "selection": b.selection, "stake": b.stake,
            "odds": b.odds, "potential_payout": b.payout, "status": b.status,
            "profit": b.profit, "placed_at": b.placed_at.isoformat() + "Z",
            "fixture": {
                "id": fx.id if fx else b.fixture_id,
                "home_name": fx.home_name if fx else "?",
                "away_name": fx.away_name if fx else "?",
                "kickoff": (fx.kickoff.isoformat() + "Z") if fx else None,
                "status": fx.status if fx else None,
                "home_score": fx.home_score if fx else None,
                "away_score": fx.away_score if fx else None,
            },
        })
    return out


# ----------------------------- leaderboard / settle-up -----------------------------

@app.get("/api/leaderboard")
def get_leaderboard(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    return settle.leaderboard(session)


@app.get("/api/settle-up")
def get_settle_up(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    return settle.settle_up(session)


# ----------------------------- leagues -----------------------------

def _member_ids(session: Session, league_id: int) -> list[int]:
    return [m.user_id for m in session.exec(
        select(LeagueMember).where(LeagueMember.league_id == league_id)
    ).all()]


def _gen_league_code(session: Session) -> str:
    import secrets
    import string
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(50):
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if session.exec(select(League).where(League.code == code)).first() is None:
            return code
    raise HTTPException(status_code=500, detail="Could not generate a unique league code")


@app.get("/api/leagues")
def my_leagues(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    memberships = session.exec(
        select(LeagueMember).where(LeagueMember.user_id == user.id)
    ).all()
    out = []
    for m in memberships:
        lg = session.get(League, m.league_id)
        if not lg:
            continue
        ids = _member_ids(session, lg.id)
        board = settle.leaderboard(session, user_ids=ids)
        mine = next((r for r in board if r["user_id"] == user.id), None)
        out.append({
            "id": lg.id,
            "name": lg.name,
            "code": lg.code,
            "is_world": lg.is_world,
            "member_count": len(ids),
            "my_rank": mine["rank"] if mine else None,
            "my_net": mine["net"] if mine else 0.0,
        })
    out.sort(key=lambda x: (not x["is_world"], x["name"].lower()))
    return out


@app.post("/api/leagues")
def create_league(body: LeagueCreateIn, session: Session = Depends(get_session),
                  user: User = Depends(get_current_user)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="League name is required")
    code = _gen_league_code(session)
    lg = League(name=name, code=code, is_world=False, created_by=user.id)
    session.add(lg)
    session.commit()
    session.refresh(lg)
    session.add(LeagueMember(league_id=lg.id, user_id=user.id))
    session.commit()
    return {"id": lg.id, "name": lg.name, "code": lg.code, "is_world": lg.is_world}


@app.post("/api/leagues/join")
def join_league(body: LeagueJoinIn, session: Session = Depends(get_session),
                user: User = Depends(get_current_user)):
    code = body.code.strip().upper()
    lg = session.exec(select(League).where(League.code == code)).first()
    if not lg:
        raise HTTPException(status_code=404, detail="No league with that code")
    existing = session.exec(
        select(LeagueMember).where(
            LeagueMember.league_id == lg.id, LeagueMember.user_id == user.id
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="You're already in this league")
    session.add(LeagueMember(league_id=lg.id, user_id=user.id))
    session.commit()
    return {"id": lg.id, "name": lg.name, "code": lg.code, "is_world": lg.is_world}


@app.get("/api/leagues/{league_id}/leaderboard")
def league_leaderboard(league_id: int, session: Session = Depends(get_session),
                       user: User = Depends(get_current_user)):
    lg = session.get(League, league_id)
    if not lg:
        raise HTTPException(status_code=404, detail="League not found")
    ids = _member_ids(session, lg.id)
    if user.id not in ids:
        raise HTTPException(status_code=403, detail="You're not in this league")
    standings = settle.leaderboard(session, user_ids=ids)
    settle_data = None if lg.is_world else settle.settle_up(session, user_ids=ids)
    return {
        "league": {"id": lg.id, "name": lg.name, "code": lg.code, "is_world": lg.is_world},
        "standings": standings,
        "settle": settle_data,
    }


# ----------------------------- admin -----------------------------

@app.get("/api/admin/status")
def admin_status(session: Session = Depends(get_session), user: User = Depends(require_admin)):
    fixtures = session.exec(select(Fixture)).all()
    upcoming = [f for f in fixtures if f.status == "NS"]
    next_fx = min(upcoming, key=lambda f: f.kickoff) if upcoming else None
    return {
        "fixtures_total": len(fixtures),
        "settled": sum(1 for f in fixtures if f.settled),
        "upcoming": len(upcoming),
        "live": sum(1 for f in fixtures if f.status == "LIVE"),
        "fixtures_json_present": sync.FIXTURES_JSON.exists(),
        "next_kickoff": (next_fx.kickoff.isoformat() + "Z") if next_fx else None,
    }


@app.post("/api/admin/import-fixtures")
def admin_import_fixtures(user: User = Depends(require_admin)):
    """One-time (or whenever needed): pull the whole schedule from ESPN into
    data/fixtures.json and the DB. This is the only fixtures call that hits the network."""
    return {"imported": sync.import_fixtures()}


@app.post("/api/admin/poll")
def admin_poll(user: User = Depends(require_admin)):
    """Fetch live scores now and settle finished matches."""
    return {"updated": sync.poll_active()}


@app.get("/api/admin/video-status")
def admin_video_status(user: User = Depends(require_admin)):
    """How many stadium clips were found and how many fixture flyovers exist."""
    return fixturevideo.status_summary()


@app.post("/api/admin/render-videos")
def admin_render_videos(background: BackgroundTasks, status: str = "NS",
                        overwrite: bool = False, user: User = Depends(require_admin)):
    """Pre-render fixture flyovers in the background (status: NS | LIVE | FT | all).
    Normally unnecessary — the scheduler renders each match ~15 min before its window
    opens — but handy to generate everything up front."""
    background.add_task(fixturevideo.render_all, status=status, overwrite=overwrite)
    return {"started": True, "note": "Rendering in the background; videos appear as they finish."}


@app.post("/api/admin/result")
def admin_result(body: ResultIn, session: Session = Depends(get_session),
                 user: User = Depends(require_admin)):
    """Manually set a final score (fallback if a match didn't auto-settle, or to
    correct one). Use force=true to re-grade an already-settled fixture."""
    fx = session.get(Fixture, body.fixture_id)
    if not fx:
        raise HTTPException(status_code=404, detail="Fixture not found")
    return settle.apply_manual_result(session, fx, body.home_score, body.away_score, body.force)


@app.post("/api/admin/elo")
def admin_set_elo(body: EloIn, session: Session = Depends(get_session),
                  user: User = Depends(require_admin)):
    team = session.get(Team, body.team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    team.elo = body.elo
    session.add(team)
    session.commit()
    return {"team_id": team.id, "name": team.name, "elo": team.elo}


@app.get("/api/health")
def health():
    return {"ok": True}


# ----------------------------- serve media (videos + stadium photos) -----------------------------

# Mounted before the SPA catch-all so /media/... resolves here. StaticFiles handles
# HTTP range requests, so the videos stream/seek properly.
app.mount("/media", StaticFiles(directory=str(fixturevideo.MEDIA_DIR)), name="media")


# ----------------------------- serve the React build (when present) -----------------------------

_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
