export const ROUTE_QUALITY_MODEL = 'NO_BACKTRACKING_OR_LOCAL_CIRCLING_V1'
const rad = value => value * Math.PI / 180
const km = (a,b) => 12742*Math.asin(Math.sqrt(Math.min(1,Math.sin(rad(b[1]-a[1])/2)**2
  +Math.cos(rad(a[1]))*Math.cos(rad(b[1]))*Math.sin(rad(b[0]-a[0])/2)**2)))
const cross = (a,b) => a[0]*b[1]-a[1]*b[0]

// Also protects old page-cache entries and display-only search prefixes.
// Re-entering the same weather cell alone is never treated as a loop.
export const assessRouteQuality = (input, speed = 10) => {
  const points=[]
  for(const point of input || []) {
    if(!Array.isArray(point) || point.length<2 || !point.slice(0,2).every(Number.isFinite)) return {acceptable:false,reason_codes:['invalid_geometry']}
    if(!points.length || km(points.at(-1),point)>1e-10) points.push(point)
  }
  if(points.length<2) return {acceptable:false,reason_codes:['invalid_geometry']}
  const lengths=points.slice(1).map((p,i)=>km(points[i],p)),offsets=[0]
  for(const length of lengths) offsets.push(offsets.at(-1)+length)
  const headings=points.slice(1).map((b,i)=>{const a=points[i];return Math.atan2((b[0]-a[0])*Math.cos(rad((a[1]+b[1])/2)),b[1]-a[1])})
  const turns=headings.slice(1).map((b,i)=>Math.abs(Math.atan2(Math.sin(b-headings[i]),Math.cos(b-headings[i])))*180/Math.PI)
  if(turns.some((angle,i)=>angle>=150 && Math.min(lengths[i],lengths[i+1])>=.05)) return {acceptable:false,reason_codes:['u_turn']}
  for(let i=0;i<points.length-1;i++) for(let j=i+1;j<points.length-1;j++) {
    const [a,b]=points.slice(i,i+2),[c,d]=points.slice(j,j+2)
    if(Math.max(a[0],b[0])+1e-9<Math.min(c[0],d[0]) || Math.max(c[0],d[0])+1e-9<Math.min(a[0],b[0])
      ||Math.max(a[1],b[1])+1e-9<Math.min(c[1],d[1]) || Math.max(c[1],d[1])+1e-9<Math.min(a[1],b[1])) continue
    const v=[b[0]-a[0],b[1]-a[1]],w=[d[0]-c[0],d[1]-c[1]],o=[c[0]-a[0],c[1]-a[1]],det=cross(v,w)
    const parameters=[]
    if(Math.abs(det)>1e-14) parameters.push([cross(o,w)/det,cross(o,v)/det])
    else if(Math.abs(cross(o,v))<=1e-13) {
      const axis=Math.abs(v[0])>=Math.abs(v[1])?0:1
      if(Math.abs(v[axis])>1e-14 && Math.abs(w[axis])>1e-14) for(const point of [a,b,c,d]) parameters.push([(point[axis]-a[axis])/v[axis],(point[axis]-c[axis])/w[axis]])
    }
    for(const [t,u] of parameters) if(t>=-1e-9&&t<=1+1e-9&&u>=-1e-9&&u<=1+1e-9
      &&offsets[j]+u*lengths[j]-offsets[i]-t*lengths[i]>=.05) return {acceptable:false,reason_codes:['spatial_cycle']}
  }
  const limit=Math.min(20,Math.max(1,Number(speed)*600/1000*2))
  for(let i=0;i<points.length-3;i++) {
    let west=points[i][0],east=west,south=points[i][1],north=south,turn=0
    for(let j=i+1;j<points.length;j++) {
      west=Math.min(west,points[j][0]);east=Math.max(east,points[j][0]);south=Math.min(south,points[j][1]);north=Math.max(north,points[j][1])
      const diameter=km([west,south],[east,north]);if(diameter>limit) break
      if(j>=i+2&&Math.min(lengths[j-2],lengths[j-1])>=.05) turn+=turns[j-2]
      const travelled=offsets[j]-offsets[i]
      if(turn>=540&&travelled>=Math.max(.1,2*diameter)&&travelled>=3*Math.max(.05,km(points[i],points[j]))) return {acceptable:false,reason_codes:['local_circling']}
    }
  }
  return {model:ROUTE_QUALITY_MODEL,acceptable:true,reason_codes:[]}
}

export const isDisplayableRoute = result => result?.display_quality?.model===ROUTE_QUALITY_MODEL
  ? result.display_quality.acceptable===true
  : assessRouteQuality(result?.points,result?.average_ground_speed_mps ?? 10).acceptable

export const qualityRetryLabel = state => {
  const quality=state?.route_quality
  if(state?.status!=='running' || quality?.status!=='searching_alternative') return undefined
  if(!Number.isFinite(quality.remaining_seconds)) return '正在额外搜索更合适的航线'
  return quality.remaining_seconds>0 ? `回避折返与盘旋，继续搜索（剩余 ${Math.ceil(quality.remaining_seconds)} 秒）` : undefined
}
