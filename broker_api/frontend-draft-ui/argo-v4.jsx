import { useState, useEffect, useRef, useCallback, createContext, useContext } from "react";

// ── Theme Definitions ──────────────────────────────────────────────────────
const THEMES = {
  DARK: {
    name: "DARK",
    bg:        "#0a0b10",
    surface:   "#0d0e14",
    surface2:  "#13141e",
    border:    "#1e2030",
    border2:   "#2a2d3e",
    text:      "#e8eaf0",
    textMuted: "#7c82a0",
    textDim:   "#a8b4d8",
    accent:    "#f0a500",
    up:        "#26d07c",
    down:      "#ff4757",
    upBg:      "#26d07c18",
    downBg:    "#ff475718",
    upBar:     "#26d07c",
    downBar:   "#ff4757",
    ceColor:   "#3b82f6",
    peColor:   "#ff4757",
    live:      "#26d07c",
  },
  CALM: {
    name: "CALM",
    bg:        "#0f1117",
    surface:   "#141720",
    surface2:  "#1a1e2a",
    border:    "#252a3a",
    border2:   "#2e3448",
    text:      "#cdd4e8",
    textMuted: "#6b7494",
    textDim:   "#8d96b8",
    accent:    "#7b8cde",
    up:        "#5b9bd5",   // blue instead of green — no fight/flight
    down:      "#b07fd4",   // purple instead of red — neutral, not alarming
    upBg:      "#5b9bd518",
    downBg:    "#b07fd418",
    upBar:     "#5b9bd5",
    downBar:   "#b07fd4",
    ceColor:   "#5b9bd5",
    peColor:   "#b07fd4",
    live:      "#5b9bd5",
  },
  LIGHT: {
    name: "LIGHT",
    bg:        "#f4f5f8",
    surface:   "#ffffff",
    surface2:  "#f0f1f5",
    border:    "#dde0ea",
    border2:   "#c8ccd8",
    text:      "#1a1d2e",
    textMuted: "#7880a0",
    textDim:   "#4a5070",
    accent:    "#d4820a",
    up:        "#1a7a4a",
    down:      "#c0392b",
    upBg:      "#1a7a4a12",
    downBg:    "#c0392b12",
    upBar:     "#1a7a4a",
    downBar:   "#c0392b",
    ceColor:   "#1a5faa",
    peColor:   "#c0392b",
    live:      "#1a7a4a",
  },
};

const ThemeCtx = createContext(THEMES.DARK);
const useTheme = () => useContext(ThemeCtx);

// ── Fake data ──────────────────────────────────────────────────────────────
const seed = (n) => Math.sin(n * 9301 + 49297) * 0.5 + 0.5;
function genCandles(base, count = 80) {
  let price = base;
  return Array.from({ length: count }, (_, i) => {
    const o = price, h = o + seed(i*3)*90, l = o - seed(i*3+1)*70;
    price = l + seed(i*3+2)*(h-l);
    return { o, h, l, c: price };
  });
}
const N_CANDLES = genCandles(23340);
const B_CANDLES = genCandles(48120);
const ATM = 23400;
const STRIKES = [23150,23200,23250,23300,23350,23400,23450,23500,23550,23600,23650,23700];

function genChain(niftyPrice) {
  return STRIKES.map((strike, i) => {
    const dist = (strike - niftyPrice) / 50;
    const ceLTP = Math.max(1, Math.round(Math.max(0, niftyPrice - strike) + 55*seed(i+1)));
    const peLTP = Math.max(1, Math.round(Math.max(0, strike - niftyPrice) + 55*seed(i+2)));
    const ceOI  = Math.round((150 - Math.abs(dist)*12 + seed(i+3)*30)*1000);
    const peOI  = Math.round((130 - Math.abs(dist)*10 + seed(i+4)*30)*1000);
    return {
      strike, isATM: Math.abs(strike - niftyPrice) < 26,
      ce: { ltp:ceLTP, oi:ceOI, delta:+Math.max(.01,Math.min(.99,.5-dist*.12)).toFixed(2), iv:+(14+seed(i+5)*8).toFixed(1), theta:-(seed(i+6)*5).toFixed(2) },
      pe: { ltp:peLTP, oi:peOI, delta:+(-1+Math.max(.01,Math.min(.99,.5-dist*.12))).toFixed(2), iv:+(13+seed(i+8)*8).toFixed(1), theta:-(seed(i+9)*5).toFixed(2) },
    };
  });
}

const SECTORS = [
  { name:"Banking",  color:"#3b82f6", stocks:[{sym:"HDFCBANK",wt:13.2,chg:0.41,pts:96},{sym:"ICICIBANK",wt:8.1,chg:-0.22,pts:-18},{sym:"KOTAKBANK",wt:4.2,chg:0.18,pts:8}]},
  { name:"IT",       color:"#8b5cf6", stocks:[{sym:"TCS",wt:5.8,chg:0.54,pts:31},{sym:"INFY",wt:6.1,chg:-0.31,pts:-19},{sym:"HCLTECH",wt:2.2,chg:0.88,pts:19}]},
  { name:"Energy",   color:"#f59e0b", stocks:[{sym:"RELIANCE",wt:9.7,chg:0.82,pts:79},{sym:"ONGC",wt:1.4,chg:-0.44,pts:-6}]},
  { name:"Auto",     color:"#10b981", stocks:[{sym:"M&M",wt:2.8,chg:1.12,pts:31},{sym:"TATAMOTORS",wt:2.1,chg:-0.88,pts:-18}]},
];

const INIT_POSITIONS = [
  {id:1,sym:"NIFTY 23400 CE",type:"CE",qty:-50,entry:145,ltp:98, expiry:"17 Apr",pnl:(98-145)*-50},
  {id:2,sym:"NIFTY 23200 PE",type:"PE",qty:-50,entry:88, ltp:112,expiry:"17 Apr",pnl:(112-88)*-50},
  {id:3,sym:"BNIFTY 48000 CE",type:"CE",qty:25, entry:210,ltp:268,expiry:"24 Apr",pnl:(268-210)*25},
];

const STRATEGIES_LIST = [
  {id:"INDEX_SL",  label:"Index Stoploss",   desc:"SL on index move"},
  {id:"INDEX_BSL", label:"Index Bracket SL", desc:"SL + target on index"},
  {id:"COMB_PREM", label:"Combined Premium ±",desc:"Exit on combined Δ"},
];

// ── Drag hooks ─────────────────────────────────────────────────────────────
function useDragV(onDrag) {
  const r = useRef({});
  return useCallback((e, init) => {
    r.current = { on:true, sy:e.clientY, init };
    const mv = (ev) => { if(r.current.on) onDrag(r.current.init + ev.clientY - r.current.sy); };
    const up = () => { r.current.on=false; window.removeEventListener("mousemove",mv); window.removeEventListener("mouseup",up); };
    window.addEventListener("mousemove",mv); window.addEventListener("mouseup",up);
  }, [onDrag]);
}
function useDragH(onDrag) {
  const r = useRef({});
  return useCallback((e, init) => {
    r.current = { on:true, sx:e.clientX, init };
    const mv = (ev) => { if(r.current.on) onDrag(r.current.init + ev.clientX - r.current.sx); };
    const up = () => { r.current.on=false; window.removeEventListener("mousemove",mv); window.removeEventListener("mouseup",up); };
    window.addEventListener("mousemove",mv); window.addEventListener("mouseup",up);
  }, [onDrag]);
}

// ── Theme Switcher ─────────────────────────────────────────────────────────
function ThemeSwitcher({ current, onChange }) {
  const t = useTheme();
  const icons = { DARK:"◐", CALM:"◑", LIGHT:"○" };
  return (
    <div style={{ display:"flex", gap:4, alignItems:"center" }}>
      {Object.values(THEMES).map(theme => (
        <button key={theme.name} onClick={() => onChange(theme.name)}
          style={{
            padding:"3px 10px", fontSize:8, fontFamily:"'Space Mono',monospace",
            letterSpacing:1, cursor:"pointer", borderRadius:4,
            border:`1px solid ${current===theme.name ? t.accent : t.border2}`,
            background: current===theme.name ? t.accent+"20" : "transparent",
            color: current===theme.name ? t.accent : t.textMuted,
            transition:"all .15s",
          }}>
          {icons[theme.name]} {theme.name}
        </button>
      ))}
    </div>
  );
}

// ── Candle Chart ───────────────────────────────────────────────────────────
function CandleChart({ candles, label, price, chg, tf, onTfChange }) {
  const t = useTheme();
  const W=500, H=118, slice=candles.slice(-60);
  const all=slice.flatMap(c=>[c.h,c.l]), mn=Math.min(...all), mx=Math.max(...all), rng=mx-mn||1;
  const py = p => H-4-((p-mn)/rng)*(H-8);
  const cw = (W-12)/slice.length;
  let ema=slice[0].c;
  const emas = slice.map(c=>{ema=ema*.86+c.c*.14; return ema;});
  return (
    <div style={{flex:1,background:t.surface,borderRadius:6,padding:"8px 12px",border:`1px solid ${t.border}`,minWidth:0,overflow:"hidden"}}>
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:4}}>
        <div style={{display:"flex",gap:10,alignItems:"baseline"}}>
          <span style={{fontSize:8,color:t.textMuted,letterSpacing:2}}>{label}</span>
          <span style={{fontSize:17,color:t.text,fontWeight:700}}>{price.toLocaleString("en-IN")}</span>
          <span style={{fontSize:9,color:chg>=0?t.up:t.down}}>{chg>=0?"▲":"▼"}{Math.abs(chg).toFixed(2)}%</span>
        </div>
        <div style={{display:"flex",gap:3}}>
          {["1m","5m","15m","1h"].map(x=>(
            <button key={x} onClick={()=>onTfChange(x)} style={{padding:"1px 5px",fontSize:8,background:tf===x?t.accent+"20":"transparent",border:`1px solid ${tf===x?t.accent:t.border2}`,color:tf===x?t.accent:t.textMuted,borderRadius:3,cursor:"pointer",fontFamily:"'Space Mono',monospace"}}>{x}</button>
          ))}
        </div>
      </div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`}>
        {slice.map((c,i)=><rect key={i} x={6+i*cw} y={H-14} width={cw*.7} height={8*seed(i)} fill={c.c>=c.o?t.upBar+"25":t.downBar+"25"}/>)}
        <polyline points={emas.map((v,i)=>`${6+i*cw+cw/2},${py(v)}`).join(" ")} fill="none" stroke={t.accent} strokeWidth="1.2" opacity=".75"/>
        {slice.map((c,i)=>{
          const x=6+i*cw+cw/2, bull=c.c>=c.o, col=bull?t.upBar:t.downBar;
          const top=py(Math.max(c.o,c.c)), bot=py(Math.min(c.o,c.c));
          return <g key={i}><line x1={x} y1={py(c.h)} x2={x} y2={py(c.l)} stroke={col} strokeWidth=".8" opacity=".6"/><rect x={x-cw*.35} y={top} width={cw*.7} height={Math.max(1,bot-top)} fill={col} opacity=".9"/></g>;
        })}
      </svg>
    </div>
  );
}

// ── Option Chain ───────────────────────────────────────────────────────────
function OptionChain({ chain, onAction, drawerStrike }) {
  const t = useTheme();
  const maxOI = Math.max(...chain.flatMap(r=>[r.ce.oi,r.pe.oi]));
  const TH = {padding:"6px 8px",textAlign:"right",borderBottom:`1px solid ${t.border}`,fontWeight:400,fontSize:9,letterSpacing:1,color:t.textMuted};
  const TD = {padding:"5px 7px",textAlign:"right",color:t.textDim,fontSize:10};
  const bBtn={background:t.upBg,border:`1px solid ${t.up}35`,color:t.up,padding:"2px 5px",borderRadius:3,cursor:"pointer",fontSize:9,fontFamily:"'Space Mono',monospace",marginRight:2};
  const sBtn={background:t.downBg,border:`1px solid ${t.down}35`,color:t.down,padding:"2px 5px",borderRadius:3,cursor:"pointer",fontSize:9,fontFamily:"'Space Mono',monospace"};
  return (
    <div style={{overflowY:"auto",height:"100%",fontFamily:"'Space Mono',monospace"}}>
      <table style={{width:"100%",borderCollapse:"collapse"}}>
        <thead>
          <tr style={{position:"sticky",top:0,background:t.bg,zIndex:2}}>
            <th style={{...TH,width:60}}>OI</th>
            <th style={{...TH,color:t.ceColor}}>CE IV</th>
            <th style={{...TH,color:t.ceColor}}>Δ</th>
            <th style={{...TH,color:t.up,fontSize:10}}>LTP</th>
            <th style={TH}>CE</th>
            <th style={{...TH,textAlign:"center",color:t.accent}}>STRIKE</th>
            <th style={TH}>PE</th>
            <th style={{...TH,color:t.down,fontSize:10}}>LTP</th>
            <th style={{...TH,color:t.peColor}}>Δ</th>
            <th style={{...TH,color:t.peColor}}>IV</th>
            <th style={{...TH,width:60}}>OI</th>
          </tr>
        </thead>
        <tbody>
          {chain.map(row=>{
            const active=drawerStrike===row.strike;
            return (
              <tr key={row.strike} style={{background:active?t.accent+"12":row.isATM?t.surface2:"transparent",borderBottom:`1px solid ${t.surface2}`}}>
                <td style={{padding:"4px 8px",width:60}}>
                  <div style={{height:5,background:t.border,borderRadius:2,overflow:"hidden"}}>
                    <div style={{width:`${(row.ce.oi/maxOI)*100}%`,height:"100%",background:t.ceColor,borderRadius:2,opacity:.7}}/>
                  </div>
                </td>
                <td style={TD}>{row.ce.iv}%</td>
                <td style={TD}>{row.ce.delta}</td>
                <td style={{...TD,color:t.up,fontWeight:600}}>{row.ce.ltp}</td>
                <td style={{padding:"4px 7px",textAlign:"right"}}>
                  <button onClick={()=>onAction(row.strike,"CE","BUY")} style={bBtn}>B</button>
                  <button onClick={()=>onAction(row.strike,"CE","SELL")} style={sBtn}>S</button>
                </td>
                <td style={{padding:"5px 10px",textAlign:"center",color:row.isATM?t.accent:t.text,fontWeight:row.isATM?700:400,borderLeft:`1px solid ${t.border}`,borderRight:`1px solid ${t.border}`,fontSize:row.isATM?12:11}}>
                  {row.strike}{row.isATM&&<sup style={{color:t.accent,fontSize:7,marginLeft:2}}>ATM</sup>}
                </td>
                <td style={{padding:"4px 7px"}}>
                  <button onClick={()=>onAction(row.strike,"PE","BUY")} style={bBtn}>B</button>
                  <button onClick={()=>onAction(row.strike,"PE","SELL")} style={sBtn}>S</button>
                </td>
                <td style={{...TD,color:t.down,textAlign:"left",fontWeight:600}}>{row.pe.ltp}</td>
                <td style={{...TD,textAlign:"left"}}>{row.pe.delta}</td>
                <td style={{...TD,textAlign:"left"}}>{row.pe.iv}%</td>
                <td style={{padding:"4px 8px",width:60}}>
                  <div style={{height:5,background:t.border,borderRadius:2,overflow:"hidden"}}>
                    <div style={{width:`${(row.pe.oi/maxOI)*100}%`,height:"100%",background:t.peColor,borderRadius:2,opacity:.7}}/>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Strategy Forms ─────────────────────────────────────────────────────────
function StrategyForms({ niftyPrice, chain, onDeploy }) {
  const t = useTheme();
  const [active,setActive]=useState("INDEX_SL");
  const atm=chain.find(r=>r.isATM)||chain[5];
  const [isl,setIsl]=useState({underlying:"NIFTY",optType:"CE",strike:atm?.strike||ATM,side:"SELL",lots:1,slPoints:30,trailSL:false});
  const [ibsl,setIbsl]=useState({underlying:"NIFTY",optType:"CE",strike:atm?.strike||ATM,side:"SELL",lots:1,slPoints:40,targetPoints:60});
  const [cpm,setCpm]=useState({legs:[{strike:atm?.strike||ATM,type:"CE",side:"SELL",lots:1},{strike:atm?.strike||ATM,type:"PE",side:"SELL",lots:1}],targetPct:50,slPct:100});

  const inputStyle={background:t.surface2,border:`1px solid ${t.border2}`,color:t.text,padding:"7px 10px",borderRadius:4,fontFamily:"'Space Mono',monospace",fontSize:11,width:"100%",boxSizing:"border-box"};
  const Field=({label,children})=>(
    <div style={{marginBottom:12}}>
      <label style={{display:"block",fontFamily:"'Space Mono',monospace",fontSize:8,color:t.textMuted,letterSpacing:1,marginBottom:5}}>{label}</label>
      {children}
    </div>
  );
  const Toggle=({options,value,onChange,colorMap={}})=>(
    <div style={{display:"flex",gap:6}}>
      {options.map(o=>{
        const col=colorMap[o]||(value===o?t.accent:t.textMuted);
        return <button key={o} onClick={()=>onChange(o)} style={{flex:1,padding:"7px 0",border:`1px solid ${value===o?col:t.border2}`,background:value===o?col+"18":"transparent",color:value===o?col:t.textMuted,borderRadius:4,cursor:"pointer",fontFamily:"'Space Mono',monospace",fontSize:10,fontWeight:value===o?600:400}}>{o}</button>;
      })}
    </div>
  );

  return (
    <div style={{display:"flex",height:"100%",overflow:"hidden",fontFamily:"'Space Mono',monospace"}}>
      {/* Sidebar */}
      <div style={{width:190,flexShrink:0,borderRight:`1px solid ${t.border}`,padding:"10px 0",overflowY:"auto",background:t.surface}}>
        <div style={{fontSize:8,color:t.textMuted,letterSpacing:2,padding:"0 14px",marginBottom:10}}>STRATEGY TYPE</div>
        {STRATEGIES_LIST.map(s=>(
          <div key={s.id} onClick={()=>setActive(s.id)} style={{padding:"10px 14px",cursor:"pointer",background:active===s.id?t.surface2:"transparent",borderLeft:`2px solid ${active===s.id?t.accent:"transparent"}`,transition:"all .15s"}}>
            <div style={{fontSize:10,color:active===s.id?t.accent:t.text,marginBottom:3}}>{s.label}</div>
            <div style={{fontSize:8,color:t.textMuted,lineHeight:1.4}}>{s.desc}</div>
          </div>
        ))}
        <div style={{borderTop:`1px solid ${t.border}`,margin:"12px 0 8px",padding:"0 14px"}}>
          <div style={{fontSize:8,color:t.textMuted,letterSpacing:2,marginBottom:8,marginTop:10}}>QUICK FIRE</div>
          {[["Short Straddle","ATM CE+PE sell"],["Short Strangle","OTM CE+PE sell"],["Iron Condor","4-leg hedge"]].map(([n,d])=>(
            <div key={n} style={{padding:"8px 0",borderBottom:`1px solid ${t.border}`,cursor:"pointer"}}>
              <div style={{fontSize:10,color:t.textDim}}>{n}</div>
              <div style={{fontSize:8,color:t.textMuted}}>{d}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Form */}
      <div style={{flex:1,overflowY:"auto",padding:"16px 20px",background:t.bg}}>

        {active==="INDEX_SL" && (
          <div>
            <div style={{fontSize:9,color:t.accent,letterSpacing:2,marginBottom:14}}>INDEX STOPLOSS ORDER</div>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:12}}>
              <Field label="UNDERLYING"><Toggle options={["NIFTY","BNIFTY"]} value={isl.underlying} onChange={v=>setIsl({...isl,underlying:v})}/></Field>
              <Field label="DIRECTION"><Toggle options={["BUY","SELL"]} value={isl.side} onChange={v=>setIsl({...isl,side:v})} colorMap={{BUY:t.up,SELL:t.down}}/></Field>
              <Field label="OPTION TYPE"><Toggle options={["CE","PE"]} value={isl.optType} onChange={v=>setIsl({...isl,optType:v})}/></Field>
              <Field label="STRIKE">
                <select value={isl.strike} onChange={e=>setIsl({...isl,strike:+e.target.value})} style={inputStyle}>
                  {STRIKES.map(s=><option key={s} value={s}>{s}{s===atm?.strike?" ATM":""}</option>)}
                </select>
              </Field>
              <Field label="LOTS">
                <div style={{display:"flex",gap:5}}>
                  {[1,2,3,5,10].map(n=><button key={n} onClick={()=>setIsl({...isl,lots:n})} style={{flex:1,padding:"6px 0",border:`1px solid ${isl.lots===n?t.accent:t.border2}`,background:isl.lots===n?t.accent+"15":"transparent",color:isl.lots===n?t.accent:t.textMuted,borderRadius:4,cursor:"pointer",fontFamily:"'Space Mono',monospace",fontSize:10}}>{n}</button>)}
                </div>
              </Field>
              <Field label="INDEX SL POINTS">
                <input type="number" value={isl.slPoints} onChange={e=>setIsl({...isl,slPoints:+e.target.value})} style={inputStyle}/>
              </Field>
            </div>
            <Field label="TRAIL STOPLOSS">
              <div style={{display:"flex",alignItems:"center",gap:10}}>
                <div onClick={()=>setIsl({...isl,trailSL:!isl.trailSL})} style={{width:36,height:18,background:isl.trailSL?t.up:t.border2,borderRadius:9,cursor:"pointer",transition:"background .2s",position:"relative",flexShrink:0}}>
                  <div style={{position:"absolute",width:12,height:12,background:"#fff",borderRadius:"50%",top:3,left:isl.trailSL?20:3,transition:"left .2s"}}/>
                </div>
                <span style={{fontSize:9,color:isl.trailSL?t.up:t.textMuted}}>{isl.trailSL?"ENABLED — trails SL with index":"DISABLED — fixed SL"}</span>
              </div>
            </Field>
            <div style={{background:t.surface,border:`1px solid ${t.border}`,borderRadius:5,padding:"10px 12px",marginBottom:14,fontSize:10}}>
              {[["INSTRUMENT",`${isl.underlying} ${isl.strike} ${isl.optType}`,t.text],["QTY",`${isl.lots*50} (${isl.lots}L)`,t.textDim],["INDEX SL",`${niftyPrice+(isl.side==="BUY"?-isl.slPoints:isl.slPoints)} (${isl.slPoints} pts)`,t.down],["TRAIL",isl.trailSL?"YES":"NO",isl.trailSL?t.up:t.textMuted]].map(([k,v,c])=>(
                <div key={k} style={{display:"flex",justifyContent:"space-between",marginBottom:4,fontFamily:"'Space Mono',monospace"}}><span style={{color:t.textMuted}}>{k}</span><span style={{color:c}}>{v}</span></div>
              ))}
            </div>
            <button onClick={()=>onDeploy("INDEX_SL",isl)} style={{width:"100%",padding:"12px 0",background:isl.side==="BUY"?t.up:t.down,border:"none",color:"#fff",borderRadius:5,fontFamily:"'Space Mono',monospace",fontSize:11,fontWeight:700,cursor:"pointer",letterSpacing:1}}>
              DEPLOY · {isl.side} {isl.lots}L {isl.underlying} {isl.strike} {isl.optType}
            </button>
          </div>
        )}

        {active==="INDEX_BSL" && (
          <div>
            <div style={{fontSize:9,color:t.accent,letterSpacing:2,marginBottom:14}}>INDEX BRACKET STOPLOSS</div>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:12}}>
              <Field label="UNDERLYING"><Toggle options={["NIFTY","BNIFTY"]} value={ibsl.underlying} onChange={v=>setIbsl({...ibsl,underlying:v})}/></Field>
              <Field label="DIRECTION"><Toggle options={["BUY","SELL"]} value={ibsl.side} onChange={v=>setIbsl({...ibsl,side:v})} colorMap={{BUY:t.up,SELL:t.down}}/></Field>
              <Field label="OPTION TYPE"><Toggle options={["CE","PE"]} value={ibsl.optType} onChange={v=>setIbsl({...ibsl,optType:v})}/></Field>
              <Field label="STRIKE">
                <select value={ibsl.strike} onChange={e=>setIbsl({...ibsl,strike:+e.target.value})} style={inputStyle}>
                  {STRIKES.map(s=><option key={s} value={s}>{s}{s===atm?.strike?" ATM":""}</option>)}
                </select>
              </Field>
              <Field label="INDEX SL POINTS"><input type="number" value={ibsl.slPoints} onChange={e=>setIbsl({...ibsl,slPoints:+e.target.value})} style={inputStyle}/></Field>
              <Field label="INDEX TARGET POINTS"><input type="number" value={ibsl.targetPoints} onChange={e=>setIbsl({...ibsl,targetPoints:+e.target.value})} style={inputStyle}/></Field>
              <Field label="LOTS">
                <div style={{display:"flex",gap:5}}>
                  {[1,2,3,5,10].map(n=><button key={n} onClick={()=>setIbsl({...ibsl,lots:n})} style={{flex:1,padding:"6px 0",border:`1px solid ${ibsl.lots===n?t.accent:t.border2}`,background:ibsl.lots===n?t.accent+"15":"transparent",color:ibsl.lots===n?t.accent:t.textMuted,borderRadius:4,cursor:"pointer",fontFamily:"'Space Mono',monospace",fontSize:10}}>{n}</button>)}
                </div>
              </Field>
            </div>
            {/* Bracket visual */}
            <div style={{background:t.surface,border:`1px solid ${t.border}`,borderRadius:5,padding:"12px 14px",marginBottom:14}}>
              <div style={{height:8,borderRadius:4,overflow:"hidden",display:"flex"}}>
                <div style={{flex:ibsl.slPoints,background:t.down+"40"}}/>
                <div style={{width:3,background:t.accent}}/>
                <div style={{flex:ibsl.targetPoints,background:t.up+"40"}}/>
              </div>
              <div style={{display:"flex",justifyContent:"space-between",marginTop:6,fontFamily:"'Space Mono',monospace",fontSize:9}}>
                <span style={{color:t.down}}>SL {niftyPrice-ibsl.slPoints}</span>
                <span style={{color:t.accent}}>NOW ~{niftyPrice}</span>
                <span style={{color:t.up}}>TGT {niftyPrice+ibsl.targetPoints}</span>
              </div>
              <div style={{display:"flex",justifyContent:"space-between",marginTop:8,fontFamily:"'Space Mono',monospace",fontSize:10}}>
                <span style={{color:t.textMuted}}>R:R</span>
                <span style={{color:ibsl.targetPoints/ibsl.slPoints>=1.5?t.up:t.down,fontWeight:700}}>1 : {(ibsl.targetPoints/ibsl.slPoints).toFixed(2)}</span>
              </div>
            </div>
            <button onClick={()=>onDeploy("INDEX_BSL",ibsl)} style={{width:"100%",padding:"12px 0",background:t.accent,border:"none",color:"#000",borderRadius:5,fontFamily:"'Space Mono',monospace",fontSize:11,fontWeight:700,cursor:"pointer",letterSpacing:1}}>
              DEPLOY BRACKET · {ibsl.underlying} {ibsl.strike} {ibsl.optType}
            </button>
          </div>
        )}

        {active==="COMB_PREM" && (
          <div>
            <div style={{fontSize:9,color:t.accent,letterSpacing:2,marginBottom:14}}>COMBINED PREMIUM ±</div>
            <div style={{marginBottom:12}}>
              <div style={{fontSize:8,color:t.textMuted,letterSpacing:1,marginBottom:8}}>LEGS</div>
              {cpm.legs.map((leg,i)=>(
                <div key={i} style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr auto auto",gap:6,marginBottom:8,alignItems:"center"}}>
                  <select value={leg.strike} onChange={e=>{const l=[...cpm.legs];l[i]={...l[i],strike:+e.target.value};setCpm({...cpm,legs:l})}} style={{...inputStyle,padding:"5px 8px",fontSize:10}}>
                    {STRIKES.map(s=><option key={s} value={s}>{s}</option>)}
                  </select>
                  <Toggle options={["CE","PE"]} value={leg.type} onChange={v=>{const l=[...cpm.legs];l[i]={...l[i],type:v};setCpm({...cpm,legs:l})}}/>
                  <Toggle options={["BUY","SELL"]} value={leg.side} onChange={v=>{const l=[...cpm.legs];l[i]={...l[i],side:v};setCpm({...cpm,legs:l})}} colorMap={{BUY:t.up,SELL:t.down}}/>
                  <div style={{display:"flex",gap:4}}>
                    {[1,2].map(n=><button key={n} onClick={()=>{const l=[...cpm.legs];l[i]={...l[i],lots:n};setCpm({...cpm,legs:l})}} style={{padding:"5px 8px",border:`1px solid ${leg.lots===n?t.accent:t.border2}`,background:leg.lots===n?t.accent+"15":"transparent",color:leg.lots===n?t.accent:t.textMuted,borderRadius:3,cursor:"pointer",fontSize:9,fontFamily:"'Space Mono',monospace"}}>{n}L</button>)}
                  </div>
                  <button onClick={()=>setCpm({...cpm,legs:cpm.legs.filter((_,j)=>j!==i)})} style={{background:t.downBg,border:`1px solid ${t.down}40`,color:t.down,padding:"5px 8px",borderRadius:3,cursor:"pointer",fontSize:11}}>✕</button>
                </div>
              ))}
              <button onClick={()=>setCpm({...cpm,legs:[...cpm.legs,{strike:atm?.strike||ATM,type:"CE",side:"SELL",lots:1}]})} style={{padding:"6px 14px",background:"transparent",border:`1px dashed ${t.border2}`,color:t.textMuted,borderRadius:4,cursor:"pointer",fontSize:9,fontFamily:"'Space Mono',monospace"}}>+ ADD LEG</button>
            </div>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:12,marginBottom:12}}>
              <Field label="PROFIT TARGET % OF ENTRY"><input type="number" value={cpm.targetPct} onChange={e=>setCpm({...cpm,targetPct:+e.target.value})} style={inputStyle}/></Field>
              <Field label="LOSS LIMIT % OF ENTRY"><input type="number" value={cpm.slPct} onChange={e=>setCpm({...cpm,slPct:+e.target.value})} style={inputStyle}/></Field>
            </div>
            {(()=>{
              const total=cpm.legs.reduce((a,l)=>a+(chain.find(r=>r.strike===l.strike)?.[l.type.toLowerCase()]?.ltp||0),0);
              return (
                <div style={{background:t.surface,border:`1px solid ${t.border}`,borderRadius:5,padding:"10px 14px",marginBottom:14,fontFamily:"'Space Mono',monospace",fontSize:10}}>
                  <div style={{display:"flex",justifyContent:"space-between",marginBottom:4}}><span style={{color:t.textMuted}}>COMBINED ENTRY</span><span style={{color:t.accent,fontWeight:700}}>{total.toFixed(1)}</span></div>
                  <div style={{display:"flex",justifyContent:"space-between",marginBottom:4}}><span style={{color:t.textMuted}}>EXIT PROFIT AT ≤</span><span style={{color:t.up}}>{(total*(1-cpm.targetPct/100)).toFixed(1)}</span></div>
                  <div style={{display:"flex",justifyContent:"space-between"}}><span style={{color:t.textMuted}}>EXIT LOSS AT ≥</span><span style={{color:t.down}}>{(total*(1+cpm.slPct/100)).toFixed(1)}</span></div>
                </div>
              );
            })()}
            <button onClick={()=>onDeploy("COMB_PREM",cpm)} style={{width:"100%",padding:"12px 0",background:t.accent,border:"none",color:"#000",borderRadius:5,fontFamily:"'Space Mono',monospace",fontSize:11,fontWeight:700,cursor:"pointer",letterSpacing:1}}>
              DEPLOY {cpm.legs.length} LEGS · COMBINED PREMIUM
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Screener ───────────────────────────────────────────────────────────────
function Screener() {
  const t = useTheme();
  const bull=SECTORS.flatMap(s=>s.stocks).filter(s=>s.pts>0).reduce((a,s)=>a+s.pts,0);
  const bear=Math.abs(SECTORS.flatMap(s=>s.stocks).filter(s=>s.pts<0).reduce((a,s)=>a+s.pts,0));
  return (
    <div style={{overflowY:"auto",height:"100%"}}>
      <div style={{marginBottom:12,padding:"8px 12px",background:t.surface,borderRadius:6,border:`1px solid ${t.border}`}}>
        <div style={{display:"flex",justifyContent:"space-between",marginBottom:5}}>
          <span style={{fontSize:8,color:t.up,fontFamily:"'Space Mono',monospace"}}>▲ {bull} pts</span>
          <span style={{fontSize:8,color:t.textMuted,fontFamily:"'Space Mono',monospace"}}>NIFTY CONSTITUENT IMPACT</span>
          <span style={{fontSize:8,color:t.down,fontFamily:"'Space Mono',monospace"}}>{bear} pts ▼</span>
        </div>
        <div style={{height:6,background:t.down+"30",borderRadius:3,overflow:"hidden"}}>
          <div style={{height:"100%",width:`${(bull/(bull+bear))*100}%`,background:t.up,borderRadius:3}}/>
        </div>
      </div>
      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(240px,1fr))",gap:10}}>
        {SECTORS.map(sec=>(
          <div key={sec.name} style={{background:t.surface,borderRadius:6,border:`1px solid ${sec.color}22`,padding:12}}>
            <div style={{fontFamily:"'Space Mono',monospace",fontSize:8,color:sec.color,letterSpacing:2,textTransform:"uppercase",marginBottom:8,paddingBottom:6,borderBottom:`1px solid ${sec.color}22`}}>{sec.name}</div>
            {sec.stocks.map(s=>(
              <div key={s.sym} style={{display:"flex",alignItems:"center",gap:6,padding:"4px 0",borderBottom:`1px solid ${t.surface2}`}}>
                <span style={{fontFamily:"'Space Mono',monospace",fontSize:10,color:t.text,width:80}}>{s.sym}</span>
                <span style={{fontFamily:"'Space Mono',monospace",fontSize:8,color:t.textMuted,width:30}}>{s.wt}%</span>
                <span style={{fontFamily:"'Space Mono',monospace",fontSize:9,color:s.chg>=0?t.up:t.down,width:48,textAlign:"right"}}>{s.chg>=0?"+":""}{s.chg}%</span>
                <div style={{flex:1,height:4,background:t.border,borderRadius:2,overflow:"hidden"}}>
                  <div style={{width:`${Math.min(100,Math.abs(s.pts))}%`,height:"100%",background:s.pts>=0?t.upBar:t.downBar,borderRadius:2}}/>
                </div>
                <span style={{fontFamily:"'Space Mono',monospace",fontSize:8,color:s.pts>=0?t.up+"99":t.down+"99",width:40,textAlign:"right"}}>{s.pts>=0?"+":""}{s.pts}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Positions Dock ─────────────────────────────────────────────────────────
function PositionsDock({ positions, onExit, expanded, onToggle }) {
  const t = useTheme();
  const totalPnl = positions.reduce((a,p)=>a+p.pnl,0);
  const wins = positions.filter(p=>p.pnl>0).length;
  return (
    <div style={{flexShrink:0,background:t.surface,borderTop:`1px solid ${t.border}`,fontFamily:"'Space Mono',monospace"}}>
      <div style={{display:"flex",alignItems:"center",gap:0,height:36,padding:"0 16px",cursor:"pointer"}} onClick={onToggle}>
        <span style={{fontSize:8,color:t.textMuted,letterSpacing:2,marginRight:12}}>POSITIONS</span>
        <div style={{display:"flex",gap:5,flex:1,overflow:"hidden"}}>
          {positions.map(p=>(
            <div key={p.id} style={{display:"flex",alignItems:"center",gap:5,background:t.surface2,border:`1px solid ${p.pnl>=0?t.up+"30":t.down+"30"}`,borderRadius:4,padding:"2px 8px",flexShrink:0}}>
              <span style={{fontSize:9,color:t.textDim}}>{p.sym.replace("NIFTY ","N·").replace("BNIFTY ","BN·")}</span>
              <span style={{fontSize:9,color:p.pnl>=0?t.up:t.down,fontWeight:700}}>{p.pnl>=0?"+":""}₹{p.pnl}</span>
            </div>
          ))}
        </div>
        <div style={{display:"flex",alignItems:"center",gap:16,marginLeft:12}}>
          <span style={{fontSize:8,color:t.textMuted}}>{wins}/{positions.length} winning</span>
          <span style={{fontSize:13,color:totalPnl>=0?t.up:t.down,fontWeight:700}}>{totalPnl>=0?"+":""}₹{totalPnl.toFixed(0)}</span>
          <span style={{fontSize:10,color:t.textMuted}}>{expanded?"▼":"▲"}</span>
        </div>
      </div>
      {expanded && (
        <div style={{padding:"0 16px 12px",borderTop:`1px solid ${t.border}`}}>
          <table style={{width:"100%",borderCollapse:"collapse",fontSize:10}}>
            <thead>
              <tr>
                {["INSTRUMENT","EXP","QTY","ENTRY","LTP","P&L",""].map(h=>(
                  <th key={h} style={{padding:"5px 8px",borderBottom:`1px solid ${t.border}`,fontWeight:400,fontSize:8,color:t.textMuted,textAlign:h==="INSTRUMENT"?"left":"right"}}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map(p=>(
                <tr key={p.id} style={{borderBottom:`1px solid ${t.surface2}`}}>
                  <td style={{padding:"6px 8px",color:t.text,textAlign:"left"}}>
                    <span style={{background:p.type==="CE"?t.up+"20":t.down+"20",color:p.type==="CE"?t.up:t.down,fontSize:7,padding:"1px 4px",borderRadius:2,marginRight:5}}>{p.type}</span>
                    {p.sym}
                  </td>
                  <td style={{padding:"6px 8px",textAlign:"right",color:t.textMuted,fontSize:8}}>{p.expiry}</td>
                  <td style={{padding:"6px 8px",textAlign:"right",color:p.qty<0?t.down:t.up}}>{p.qty>0?"+":""}{p.qty}</td>
                  <td style={{padding:"6px 8px",textAlign:"right",color:t.textDim}}>{p.entry}</td>
                  <td style={{padding:"6px 8px",textAlign:"right",color:t.text}}>{p.ltp}</td>
                  <td style={{padding:"6px 8px",textAlign:"right",color:p.pnl>=0?t.up:t.down,fontWeight:700}}>{p.pnl>=0?"+":""}₹{p.pnl}</td>
                  <td style={{padding:"6px 8px",textAlign:"right"}}>
                    <button onClick={()=>onExit(p.id)} style={{background:t.downBg,border:`1px solid ${t.down}`,color:t.down,padding:"2px 8px",borderRadius:3,fontSize:8,cursor:"pointer",fontFamily:"'Space Mono',monospace"}}>EXIT</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Order Drawer ───────────────────────────────────────────────────────────
function OrderDrawer({ open, onClose, strike:initS, type:initT, side:initD }) {
  const t = useTheme();
  const [side,setSide]=useState(initD||"BUY");
  const [type,setType]=useState(initT||"CE");
  const [strike,setStrike]=useState(initS||ATM);
  const [lots,setLots]=useState(1);
  const [oType,setOType]=useState("MARKET");
  const [placed,setPlaced]=useState(false);
  useEffect(()=>{if(initS)setStrike(initS);},[initS]);
  useEffect(()=>{if(initT)setType(initT);},[initT]);
  useEffect(()=>{if(initD)setSide(initD);},[initD]);
  const inp={background:t.surface2,border:`1px solid ${t.border2}`,color:t.text,padding:"7px 10px",borderRadius:4,fontFamily:"'Space Mono',monospace",fontSize:11,width:"100%",boxSizing:"border-box",marginBottom:12};
  return (
    <div style={{position:"absolute",right:0,top:0,bottom:0,width:open?270:0,background:t.surface,borderLeft:`1px solid ${t.border}`,transition:"width .22s cubic-bezier(.4,0,.2,1)",overflow:"hidden",zIndex:20,display:"flex",flexDirection:"column"}}>
      <div style={{padding:"12px 14px",borderBottom:`1px solid ${t.border}`,display:"flex",justifyContent:"space-between",alignItems:"center",flexShrink:0}}>
        <span style={{fontFamily:"'Space Mono',monospace",fontSize:9,color:t.accent,letterSpacing:2}}>ORDER TICKET</span>
        <button onClick={onClose} style={{background:"transparent",border:"none",color:t.textMuted,cursor:"pointer",fontSize:15}}>✕</button>
      </div>
      <div style={{flex:1,overflowY:"auto",padding:14}}>
        <div style={{display:"flex",gap:6,marginBottom:12}}>
          {["BUY","SELL"].map(s=><button key={s} onClick={()=>setSide(s)} style={{flex:1,padding:"8px 0",border:`1px solid ${side===s?(s==="BUY"?t.up:t.down):t.border2}`,background:side===s?(s==="BUY"?t.upBg:t.downBg):"transparent",color:side===s?(s==="BUY"?t.up:t.down):t.textMuted,borderRadius:4,cursor:"pointer",fontFamily:"'Space Mono',monospace",fontSize:11,fontWeight:700}}>{s}</button>)}
        </div>
        <div style={{display:"flex",gap:6,marginBottom:12}}>
          {["CE","PE"].map(x=><button key={x} onClick={()=>setType(x)} style={{flex:1,padding:"6px 0",border:`1px solid ${type===x?t.accent:t.border2}`,background:type===x?t.accent+"15":"transparent",color:type===x?t.accent:t.textMuted,borderRadius:4,cursor:"pointer",fontFamily:"'Space Mono',monospace",fontSize:10}}>{x}</button>)}
        </div>
        <label style={{display:"block",fontFamily:"'Space Mono',monospace",fontSize:8,color:t.textMuted,letterSpacing:1,marginBottom:5}}>STRIKE</label>
        <select value={strike} onChange={e=>setStrike(+e.target.value)} style={inp}>
          {STRIKES.map(s=><option key={s} value={s}>{s}{s===ATM?" ATM":""}</option>)}
        </select>
        <label style={{display:"block",fontFamily:"'Space Mono',monospace",fontSize:8,color:t.textMuted,letterSpacing:1,marginBottom:5}}>ORDER TYPE</label>
        <div style={{display:"flex",gap:5,marginBottom:12}}>
          {["MARKET","LIMIT","SL-M"].map(x=><button key={x} onClick={()=>setOType(x)} style={{flex:1,padding:"5px 0",border:`1px solid ${oType===x?t.textDim:t.border2}`,background:oType===x?t.border2:"transparent",color:oType===x?t.text:t.textMuted,borderRadius:3,cursor:"pointer",fontFamily:"'Space Mono',monospace",fontSize:8}}>{x}</button>)}
        </div>
        <label style={{display:"block",fontFamily:"'Space Mono',monospace",fontSize:8,color:t.textMuted,letterSpacing:1,marginBottom:5}}>LOTS</label>
        <div style={{display:"flex",gap:5,marginBottom:14}}>
          {[1,2,3,5,10].map(n=><button key={n} onClick={()=>setLots(n)} style={{flex:1,padding:"6px 0",border:`1px solid ${lots===n?t.accent:t.border2}`,background:lots===n?t.accent+"15":"transparent",color:lots===n?t.accent:t.textMuted,borderRadius:4,cursor:"pointer",fontFamily:"'Space Mono',monospace",fontSize:10}}>{n}</button>)}
        </div>
        <div style={{background:t.surface2,borderRadius:5,padding:"10px 12px",marginBottom:14,fontFamily:"'Space Mono',monospace",fontSize:10}}>
          <div style={{display:"flex",justifyContent:"space-between",marginBottom:3}}><span style={{color:t.textMuted}}>QTY</span><span style={{color:t.text}}>{lots*50}</span></div>
          <div style={{display:"flex",justifyContent:"space-between"}}><span style={{color:t.textMuted}}>EST. MARGIN</span><span style={{color:t.accent}}>₹{(lots*12400).toLocaleString("en-IN")}</span></div>
        </div>
        <button onClick={()=>{setPlaced(true);setTimeout(()=>{setPlaced(false);onClose();},1200);}} style={{width:"100%",padding:"11px 0",background:placed?t.up:(side==="BUY"?t.up:t.down),border:"none",color:"#fff",borderRadius:5,fontFamily:"'Space Mono',monospace",fontSize:11,fontWeight:700,cursor:"pointer",letterSpacing:1}}>
          {placed?"✓ PLACED":`${side} ${type} ${strike}`}
        </button>
      </div>
    </div>
  );
}

// ── Main ───────────────────────────────────────────────────────────────────
const TABS = ["OPTION CHAIN","STRATEGIES","SCREENER"];

export default function Argo() {
  const [themeName,setThemeName]=useState("DARK");
  const theme = THEMES[themeName];
  const [tab,setTab]=useState("OPTION CHAIN");
  const [drawerOpen,setDrawerOpen]=useState(false);
  const [drawerStrike,setDrawerStrike]=useState(null);
  const [drawerType,setDrawerType]=useState("CE");
  const [drawerSide,setDrawerSide]=useState("BUY");
  const [chartH,setChartH]=useState(180);
  const [splitPct,setSplitPct]=useState(50);
  const [posExpanded,setPosExpanded]=useState(false);
  const [positions,setPositions]=useState(INIT_POSITIONS);
  const [niftyPrice,setNiftyPrice]=useState(23452);
  const [bniftyPrice,setBniftyPrice]=useState(48238);
  const [nTf,setNTf]=useState("15m");
  const [bTf,setBTf]=useState("15m");
  const [deployed,setDeployed]=useState(null);
  const containerRef=useRef(null);

  useEffect(()=>{
    const id=setInterval(()=>{
      setNiftyPrice(p=>Math.max(23000,Math.min(24000,p+Math.round((Math.random()-.48)*12))));
      setBniftyPrice(p=>Math.max(47000,Math.min(49500,p+Math.round((Math.random()-.48)*22))));
    },1000);
    return ()=>clearInterval(id);
  },[]);

  const chain=genChain(niftyPrice);
  const nChg=((niftyPrice-23340)/23340)*100;
  const bChg=((bniftyPrice-48120)/48120)*100;
  const totalPnl=positions.reduce((a,p)=>a+p.pnl,0);

  const onVDrag=useCallback(v=>setChartH(Math.max(100,Math.min(380,v))),[]);
  const onHDrag=useCallback(v=>{if(!containerRef.current)return;setSplitPct(Math.max(25,Math.min(75,(v/containerRef.current.offsetWidth)*100)));},[]);
  const vDrag=useDragV(onVDrag);
  const hDrag=useDragH(onHDrag);
  const openDrawer=(strike,type,side)=>{setDrawerStrike(strike);setDrawerType(type);setDrawerSide(side);setDrawerOpen(true);};

  return (
    <ThemeCtx.Provider value={theme}>
      <div style={{background:theme.bg,height:"100vh",color:theme.text,display:"flex",flexDirection:"column",fontFamily:"'Space Mono',monospace",overflow:"hidden",userSelect:"none"}}>
        {/* Header */}
        <div style={{display:"flex",alignItems:"center",padding:"0 16px",height:42,background:theme.surface,borderBottom:`1px solid ${theme.border}`,gap:16,flexShrink:0}}>
          <span style={{fontSize:14,fontWeight:700,color:theme.accent,letterSpacing:3}}>ARGO</span>
          <div style={{width:1,height:18,background:theme.border}}/>
          {[{l:"NIFTY 50",p:niftyPrice,c:nChg},{l:"BANKNIFTY",p:bniftyPrice,c:bChg}].map(m=>(
            <div key={m.l} style={{display:"flex",gap:8,alignItems:"baseline"}}>
              <span style={{fontSize:8,color:theme.textMuted,letterSpacing:1}}>{m.l}</span>
              <span style={{fontSize:13,color:theme.text,fontWeight:700}}>{m.p.toLocaleString("en-IN")}</span>
              <span style={{fontSize:9,color:m.c>=0?theme.up:theme.down}}>{m.c>=0?"▲":"▼"}{Math.abs(m.c).toFixed(2)}%</span>
            </div>
          ))}
          <div style={{marginLeft:"auto",display:"flex",gap:14,alignItems:"center"}}>
            {/* Theme switcher */}
            <ThemeSwitcher current={themeName} onChange={setThemeName}/>
            <div style={{width:1,height:18,background:theme.border}}/>
            <div style={{textAlign:"right"}}>
              <div style={{fontSize:7,color:theme.textMuted}}>DAY P&L</div>
              <div style={{fontSize:13,color:totalPnl>=0?theme.up:theme.down,fontWeight:700}}>{totalPnl>=0?"+":""}₹{totalPnl.toFixed(0)}</div>
            </div>
            <button onClick={()=>openDrawer(ATM,"CE","BUY")} style={{padding:"4px 12px",background:theme.accent+"18",border:`1px solid ${theme.accent}`,color:theme.accent,borderRadius:4,cursor:"pointer",fontSize:8,fontFamily:"'Space Mono',monospace",letterSpacing:1}}>+ ORDER</button>
            <div style={{display:"flex",gap:4,alignItems:"center"}}>
              <div style={{width:6,height:6,borderRadius:"50%",background:theme.live,boxShadow:`0 0 6px ${theme.live}`}}/>
              <span style={{fontSize:8,color:theme.live}}>LIVE</span>
            </div>
          </div>
        </div>

        {/* Charts */}
        <div ref={containerRef} style={{flexShrink:0,padding:"10px 16px 0"}}>
          <div style={{display:"flex",height:chartH}}>
            <div style={{width:`${splitPct}%`,paddingRight:5}}>
              <CandleChart candles={N_CANDLES} label="NIFTY 50" price={niftyPrice} chg={nChg} tf={nTf} onTfChange={setNTf}/>
            </div>
            <div onMouseDown={e=>hDrag(e,(splitPct/100)*(containerRef.current?.offsetWidth||1000))} style={{width:6,cursor:"col-resize",display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0}}>
              <div style={{width:2,height:36,background:theme.border2,borderRadius:2}}/>
            </div>
            <div style={{flex:1,paddingLeft:5}}>
              <CandleChart candles={B_CANDLES} label="BANKNIFTY" price={bniftyPrice} chg={bChg} tf={bTf} onTfChange={setBTf}/>
            </div>
          </div>
          <div onMouseDown={e=>vDrag(e,chartH)} style={{height:8,cursor:"row-resize",display:"flex",alignItems:"center",justifyContent:"center",margin:"2px 0"}}>
            <div style={{width:50,height:2,background:theme.border2,borderRadius:2}}/>
          </div>
        </div>

        {/* Tabs */}
        <div style={{display:"flex",borderTop:`1px solid ${theme.border}`,borderBottom:`1px solid ${theme.border}`,flexShrink:0,background:theme.surface}}>
          {TABS.map(x=>(
            <button key={x} onClick={()=>setTab(x)} style={{padding:"7px 18px",border:"none",cursor:"pointer",fontSize:9,letterSpacing:1.5,fontFamily:"'Space Mono',monospace",background:"transparent",color:tab===x?theme.accent:theme.textMuted,borderBottom:tab===x?`2px solid ${theme.accent}`:"2px solid transparent"}}>
              {x}
            </button>
          ))}
          <div style={{flex:1}}/>
          <div style={{display:"flex",alignItems:"center",padding:"0 14px",gap:10,fontSize:8,color:theme.textMuted}}>
            <span>PCR <span style={{color:theme.up}}>1.12</span></span>
            <span style={{color:theme.border2}}>│</span>
            <span>VIX <span style={{color:theme.accent}}>14.2</span></span>
            <span style={{color:theme.border2}}>│</span>
            <span>EXP <span style={{color:theme.textDim}}>17 Apr</span></span>
          </div>
        </div>

        {/* Active zone */}
        <div style={{flex:1,overflow:"hidden",position:"relative",display:"flex",minHeight:0}}>
          <div style={{flex:1,overflow:"hidden",padding:"10px 16px",paddingRight:drawerOpen?286:16,transition:"padding-right .22s"}}>
            {tab==="OPTION CHAIN" && <OptionChain chain={chain} onAction={openDrawer} drawerStrike={drawerStrike}/>}
            {tab==="STRATEGIES"  && <StrategyForms niftyPrice={niftyPrice} chain={chain} onDeploy={(s,p)=>{setDeployed({s,p});setTimeout(()=>setDeployed(null),2500);}}/>}
            {tab==="SCREENER"    && <Screener/>}
          </div>
          <OrderDrawer open={drawerOpen} onClose={()=>setDrawerOpen(false)} strike={drawerStrike} type={drawerType} side={drawerSide}/>
        </div>

        {/* Deploy toast */}
        {deployed && (
          <div style={{position:"fixed",top:52,right:20,background:theme.up,color:"#fff",padding:"8px 16px",borderRadius:6,fontFamily:"'Space Mono',monospace",fontSize:10,fontWeight:700,zIndex:100,boxShadow:`0 4px 20px ${theme.up}40`}}>
            ✓ STRATEGY DEPLOYED · {deployed.s}
          </div>
        )}

        {/* Positions dock */}
        <PositionsDock positions={positions} onExit={id=>setPositions(ps=>ps.filter(p=>p.id!==id))} expanded={posExpanded} onToggle={()=>setPosExpanded(p=>!p)}/>
      </div>
    </ThemeCtx.Provider>
  );
}
