import { useEffect, useRef, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { getFixture, getFixtureVideo, placeBet } from '../api.js'
import { matchStadium, COUNTRY_NAMES } from '../data.js'
import MapNA from '../MapNA.jsx'
import Countdown from '../Countdown.jsx'

const STAKES = [100, 200, 300, 500]

function fmt(iso) {
  return new Date(iso).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function labelFor(f, market, selection) {
  if (market === '1X2') {
    const lab = selection === 'HOME' ? f.home.name : selection === 'AWAY' ? f.away.name : 'Draw'
    return `${lab} to ${selection === 'DRAW' ? 'draw' : 'win'}`
  }
  const [i, j] = selection.split('-')
  return `Exact score ${f.home.name} ${i}–${j} ${f.away.name}`
}
function Flag({ src, big }) {
  if (!src) return <span className={'flag'} style={big ? { width: 56, height: 40 } : null} />
  return <img className="flag" style={big ? { width: 56, height: 40 } : null} src={src} alt="" onError={(e) => { e.currentTarget.style.visibility = 'hidden' }} />
}

export default function FixtureDetail() {
  const { id } = useParams()
  const [f, setF] = useState(null)
  const [err, setErr] = useState('')
  const [sel, setSel] = useState(null)       // { market, selection, odds, label }
  const [stake, setStake] = useState(100)
  const [msg, setMsg] = useState(null)       // { kind, text }
  const [busy, setBusy] = useState(false)

  // flyover intro
  const [videoUrl, setVideoUrl] = useState(undefined)  // undefined=checking, null=none, string=url
  const [introDone, setIntroDone] = useState(false)
  const [replay, setReplay] = useState(false)
  const [muted, setMuted] = useState(false)
  const introRef = useRef(null)

  useEffect(() => {
    setErr(''); setF(null); setSel(null); setMsg(null)
    setVideoUrl(undefined); setIntroDone(false); setReplay(false); setMuted(false)
    getFixture(id).then(setF).catch(e => setErr(e.message))
    getFixtureVideo(id).then(v => setVideoUrl(v?.url || null)).catch(() => setVideoUrl(null))
  }, [id])

  // Try to play the flyover WITH sound. Browsers block unmuted autoplay unless there's a
  // user gesture, so if that's refused we fall back to muted playback and unmute on the
  // very first interaction (click/tap/key) — the earliest the browser will allow sound.
  useEffect(() => {
    const v = introRef.current
    if (!v || !videoUrl || introDone) return
    let cleanup = () => {}
    v.muted = false
    const p = v.play()
    if (p && p.catch) {
      p.catch(() => {
        v.muted = true; setMuted(true)
        v.play().catch(() => {})
        const unmute = () => { const el = introRef.current; if (el) el.muted = false; setMuted(false) }
        window.addEventListener('pointerdown', unmute, { once: true })
        window.addEventListener('keydown', unmute, { once: true })
        cleanup = () => {
          window.removeEventListener('pointerdown', unmute)
          window.removeEventListener('keydown', unmute)
        }
      })
    }
    return () => cleanup()
  }, [videoUrl, introDone])

  // Pre-select the user's existing bet (if any) so the page opens on their current pick.
  useEffect(() => {
    if (f && f.my_bet) {
      setStake(f.my_bet.stake)
      setSel({
        market: f.my_bet.market,
        selection: f.my_bet.selection,
        odds: f.my_bet.odds,
        label: labelFor(f, f.my_bet.market, f.my_bet.selection),
      })
    }
  }, [f])

  if (err) return <div className="container"><div className="msg err">{err}</div></div>
  if (videoUrl === undefined) return (
    <div className="container">
      <Link to="/" className="muted" style={{ fontSize: 14 }}>← All fixtures</Link>
      <div className="intro-hero" style={{ marginTop: 14, aspectRatio: '16 / 9' }}>
        <div className="intro-prep"><div className="spinner" /><div>Preparing the stadium flyover…</div></div>
      </div>
    </div>
  )

  // The flyover is the entrance: play it once, then reveal the betting view.
  if (videoUrl && !introDone) {
    return (
      <div className="container">
        <Link to="/" className="muted" style={{ fontSize: 14 }}>← All fixtures</Link>
        <div className="intro-hero" style={{ marginTop: 14 }}>
          <video ref={introRef} className="intro-video" src={videoUrl}
            muted={muted} playsInline preload="auto"
            onEnded={() => setIntroDone(true)} />
          <button className="intro-icon" title={muted ? 'Unmute' : 'Mute'}
            onClick={() => { const v = introRef.current; if (v) { v.muted = !muted } setMuted(m => !m) }}>
            {muted ? '🔇' : '🔊'}
          </button>
          <button className="intro-skip" onClick={() => setIntroDone(true)}>Skip to betting ▸</button>
        </div>
      </div>
    )
  }

  if (!f) return <div className="container"><div className="loading">Loading match…</div></div>

  const stadium = matchStadium(f.venue, f.city)
  const finished = f.status === 'FT' || f.settled
  const canBet = f.betting_open
  const payout = sel ? +(stake * sel.odds).toFixed(2) : 0

  function pick(market, selection, odds, label) {
    setMsg(null)
    setSel({ market, selection, odds, label })
  }

  async function confirm() {
    if (!sel) return
    setBusy(true); setMsg(null)
    try {
      const res = await placeBet({ fixture_id: f.id, market: sel.market, selection: sel.selection, stake })
      const verb = res?.replaced ? 'updated' : 'placed'
      setMsg({ kind: 'ok', text: `Bet ${verb}: ${sel.label} for ${stake} ₹ at ${sel.odds} (returns ${payout} ₹).` })
      const fresh = await getFixture(f.id)
      setF(fresh)
    } catch (e) {
      setMsg({ kind: 'err', text: e.message })
    } finally { setBusy(false) }
  }

  return (
    <div className="container">
      <Link to="/" className="muted" style={{ fontSize: 14 }}>← All fixtures</Link>

      <div className="panel" style={{ marginTop: 14 }}>
        <div className="spread">
          <span className="muted" style={{ fontSize: 13 }}>{f.stage || 'Match'}</span>
          <span className="muted" style={{ fontSize: 13 }}>{fmt(f.kickoff)}</span>
        </div>
        <div className="fx-head">
          <div className="fx-team"><Flag src={f.home.logo} big /><b>{f.home.name}</b></div>
          <div className="fx-mid">
            {finished
              ? <div className="big">{f.home_score} – {f.away_score}</div>
              : <div className="big muted2">vs</div>}
            {f.status === 'LIVE' && <span className="badge live">Live</span>}
            {finished && <span className="badge ft">Full time</span>}
          </div>
          <div className="fx-team"><Flag src={f.away.logo} big /><b>{f.away.name}</b></div>
        </div>
        <div style={{ textAlign: 'center' }} className="muted">
          {f.venue}{f.city ? ` · ${f.city}` : ''}
        </div>
      </div>

      <div className="grid2" style={{ marginTop: 16 }}>
          {/* markets */}
          <div className="panel">
            <p className="section-label">Match result</p>
            <div className="odds-row">
              {[['HOME', f.home.name], ['DRAW', 'Draw'], ['AWAY', f.away.name]].map(([k, lab]) => (
                <button key={k}
                  className={'odd-btn' + (sel?.market === '1X2' && sel?.selection === k ? ' sel' : '')}
                  disabled={!canBet}
                  onClick={() => pick('1X2', k, f.odds[k], `${lab} to ${k === 'DRAW' ? 'draw' : 'win'}`)}>
                  <div className="lab">{lab}</div>
                  <div className="val">{f.odds[k] ?? '–'}</div>
                </button>
              ))}
            </div>

            <p className="section-label" style={{ marginTop: 22 }}>Exact scoreline · higher payout</p>
            <div className="scoregrid">
              <div className="sg-h" style={{ color: '#69728f', fontSize: 10 }}>H＼A</div>
              {[0, 1, 2, 3, 4, 5].map(j => <div key={'h' + j} className="sg-h">{j}</div>)}
              {[0, 1, 2, 3, 4, 5].map(i => (
                <Row key={'r' + i} i={i} f={f} sel={sel} canBet={canBet} pick={pick} />
              ))}
            </div>
            <p className="muted2" style={{ fontSize: 12, marginTop: 10 }}>Rows = {f.home.name} goals · columns = {f.away.name} goals</p>
          </div>

          {/* bet slip + stadium */}
          <div>
            <div className="panel">
              <p className="section-label">Bet slip</p>
              {f.my_bet && (
                <div className="slip-line" style={{ borderBottom: '1px solid var(--line)', paddingBottom: 10, marginBottom: 4 }}>
                  <span className="muted">Your current bet</span>
                  <b>{labelFor(f, f.my_bet.market, f.my_bet.selection)} · {f.my_bet.stake} ₹ @ {f.my_bet.odds}</b>
                </div>
              )}
              {!canBet && (
                <div className="msg err" style={{ marginTop: 0 }}>
                  {finished ? 'This match is over — betting closed.'
                    : f.status === 'LIVE' ? 'Match in progress — betting closed.'
                    : <>Betting isn’t open. <Countdown target={f.closes_at} closedLabel="window closed" prefix="closes in " /></>}
                </div>
              )}
              {canBet && !sel && <p className="muted" style={{ fontSize: 14 }}>Pick a result or scoreline to build your bet.</p>}
              {canBet && sel && (
                <>
                  <div className="slip-line"><span>{sel.label}</span><b>{sel.odds}</b></div>
                  <div className="field" style={{ margin: '12px 0 0' }}>
                    <label className="muted" style={{ fontSize: 13 }}>Stake (100–500 ₹)</label>
                    <input className="stake-input" type="number" min="100" max="500" value={stake}
                      onChange={e => setStake(Math.max(100, Math.min(500, +e.target.value || 0)))} />
                  </div>
                  <div className="chips">
                    {STAKES.map(s => <button key={s} className="chip" onClick={() => setStake(s)}>{s}</button>)}
                  </div>
                  <div className="slip-line" style={{ borderTop: '1px solid var(--line)', paddingTop: 10 }}>
                    <span className="muted">Potential return</span><b>{payout} ₹</b>
                  </div>
                  <div className="slip-line"><span className="muted">Profit if it lands</span>
                    <b style={{ color: 'var(--green)' }}>+{(payout - stake).toFixed(2)} ₹</b></div>
                  <button className="btn btn-primary" style={{ width: '100%', marginTop: 12 }} disabled={busy} onClick={confirm}>
                    {busy ? (f.my_bet ? 'Updating…' : 'Placing…') : (f.my_bet ? 'Update bet' : 'Place bet')}
                  </button>
                </>
              )}
              {msg && <div className={'msg ' + (msg.kind === 'ok' ? 'ok' : 'err')}>{msg.text}</div>}
            </div>

            <div className="panel" style={{ marginTop: 16 }}>
              <div className="spread" style={{ marginBottom: 12 }}>
                <p className="section-label" style={{ margin: 0 }}>Stadium</p>
                {videoUrl && (
                  <button className="btn btn-small" onClick={() => setReplay(r => !r)}>
                    {replay ? 'Show map' : '▶ Replay flyover'}
                  </button>
                )}
              </div>
              {replay && videoUrl
                ? <video className="venue-video" src={videoUrl} autoPlay controls playsInline />
                : <MapNA highlightVenue={stadium?.venue || f.venue} height={320} />}
              <div style={{ marginTop: 12 }}>
                <b>{f.venue || (stadium && stadium.venue) || 'TBD'}</b>
                <div className="muted" style={{ fontSize: 13 }}>
                  {f.city || (stadium && stadium.city) || ''}
                  {stadium ? ` · ${COUNTRY_NAMES[stadium.country]} · ${stadium.cap.toLocaleString()} seats` : (f.country ? ` · ${f.country}` : '')}
                </div>
              </div>
            </div>
          </div>
        </div>
    </div>
  )
}

function Row({ i, f, sel, canBet, pick }) {
  return (
    <>
      <div className="sg-h">{i}</div>
      {[0, 1, 2, 3, 4, 5].map(j => {
        const key = `${i}-${j}`
        const odds = f.scorelines?.[key]
        const seld = sel?.market === 'SCORELINE' && sel?.selection === key
        return (
          <div key={key} className={'sg-cell' + (seld ? ' sel' : '')}
            onClick={() => canBet && odds && pick('SCORELINE', key, odds, `Exact score ${f.home.name} ${i}–${j} ${f.away.name}`)}
            style={{ opacity: canBet ? 1 : 0.5, cursor: canBet ? 'pointer' : 'default' }}>
            <div className="s">{i}–{j}</div>
            <div className="o">{odds ?? '–'}</div>
          </div>
        )
      })}
    </>
  )
}
