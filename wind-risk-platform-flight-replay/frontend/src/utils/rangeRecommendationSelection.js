// Selection changes the displayed, saveable fixed route, never the search task.
const time = value => Date.parse(value)
export const rangeCardProfileKey = card => JSON.stringify([card.id, card.plan.dataset_id, card.plan.source,
  card.plan.level, card.plan.average_ground_speed_mps, time(card.recommended_departure_at || card.first_departure_at)])
const viewKey = (card, workflow) => JSON.stringify([rangeCardProfileKey(card), card.windows,
  card.max_contact_wind_speed_mps, workflow.peak_wind_limit_mps])
const groupKey = workflow => JSON.stringify([workflow?.dataset_id, workflow?.baseline_version,
  workflow?.departure_start_at, workflow?.departure_end_at])

export function isCurrentRangeCard(card, workflow, now=Date.now()) {
  const plan = card?.plan, departure = time(card?.recommended_departure_at || card?.first_departure_at)
  const peak = card?.max_contact_wind_speed_mps ?? plan?.max_contact_wind_speed_mps
  return Boolean(workflow?.mode === 'departure_range' && plan?.points?.length >= 2
    && plan.dataset_id === workflow.dataset_id && Number.isFinite(departure) && departure >= now
    && departure >= time(workflow.departure_start_at) && departure <= time(workflow.departure_end_at)
    && Number.isFinite(peak) && peak <= workflow.peak_wind_limit_mps)
}

export function rangeSelectionResult(card, profile, workflow, now=Date.now()) {
  if (!isCurrentRangeCard(card, workflow, now)) throw Error('该方案已更新或起飞时刻已过，请选择当前推荐方案。')
  const plan = card.plan, departure = card.recommended_departure_at || card.first_departure_at
  if (profile.dataset_id !== plan.dataset_id || profile.level !== plan.level
    || profile.source !== plan.source
    || time(profile.departure_at) !== time(departure)
    || Math.abs(profile.average_ground_speed_mps-plan.average_ground_speed_mps) > 1e-9
    || !Number.isFinite(profile.average_ground_speed_mps) || profile.analysis?.navigation_allowed !== true) {
    throw Error('所选方案的时刻、数据版本或全程复核不一致，请重新推荐。')
  }
  const peak = card.max_contact_wind_speed_mps ?? plan.max_contact_wind_speed_mps
  const analysis = {...profile.analysis, interpolated_max_wind_speed:profile.analysis.interpolated_max_wind_speed ?? profile.analysis.max_wind_speed,
    max_wind_speed:peak, max_contact_wind_speed_mps:peak, peak_wind_model:'CLOSED_CELL_CONTACT_PEAK_V1'}
  const windows = card.windows?.low_wind_certified ? card.windows : null
  if (windows && (windows.dataset_id !== plan.dataset_id || windows.level && windows.level!==plan.level
    || windows.source && windows.source!==plan.source || Math.abs(windows.peak_wind_limit_mps-workflow.peak_wind_limit_mps)>1e-9)) {
    throw Error('该方案的低风窗口已更新，请重新选择。')
  }
  return {...plan, mode:'departure_range', analysis, flight_profile:{...profile,analysis}, segments:profile.segments,
    departure_at:profile.departure_at, arrival_at:profile.arrival_at,
    max_contact_wind_speed_mps:peak, departure_windows:windows,
    selected_recommendation_id:card.id, selection_revision:workflow.selection_revision,
    selection_model:workflow.selection_model, min_verified_peak_mps:workflow.min_verified_peak_mps,
    peak_wind_limit_mps:workflow.peak_wind_limit_mps, range_profile_key:rangeCardProfileKey(card),
    route_context:{...plan.route_context,points:plan.points,dataset_id:plan.dataset_id,source:plan.source,
      level:plan.level,ephemeral:true,departure_at:profile.departure_at}}
}

export function createRangeRecommendationSelection({requestProfile,onState,onSelect,onPause=()=>{},onDefault=()=>false,
  onError=()=>{},now=Date.now,maxCachedProfiles=3}) {
  let workflow, intentId, currentId, committedId, pendingId, pendingKey, controller, activeProfile, activeKey, appliedKey
  let generation=0, disposed=false, lastGroup, lastRevision=-1
  const cache=new Map()
  const find = id => workflow?.recommendations?.find(card=>card.id===id)
  const first = () => workflow?.recommendations?.find(card=>isCurrentRangeCard(card,workflow,now()))
  const emit = () => { if(!disposed) onState?.({selectedId:currentId || null,
    pendingId:pendingId || null,manual:Boolean(intentId)}) }
  const abort = () => { generation++;controller?.abort();controller=undefined;pendingId=undefined;pendingKey=undefined }
  const put = (key,profile) => {
    cache.delete(key);cache.set(key,profile)
    while(cache.size>maxCachedProfiles) cache.delete(cache.keys().next().value)
  }
  const apply = (card,profile) => {
    const result=rangeSelectionResult(card,profile,workflow,now())
    currentId=card.id;activeProfile=profile;activeKey=rangeCardProfileKey(card);appliedKey=viewKey(card,workflow)
    onSelect(result,card,workflow);emit()
  }
  const restoreDefault = (message) => {
    abort();intentId=undefined;committedId=undefined;currentId=first()?.id;activeKey=undefined;activeProfile=undefined;appliedKey=undefined
    emit()
    if(message) onError(message)
    if(!onDefault(workflow) && first()) void select(first().id,false)
  }
  async function select(id,manual=true) {
    const card=find(id)
    if(disposed || !isCurrentRangeCard(card,workflow,now())) {
      if(!disposed) onError('该方案已更新或起飞时刻已过，请选择当前推荐方案。')
      return false
    }
    const key=rangeCardProfileKey(card)
    if(pendingId===id && pendingKey===key) return false
    if(currentId===id && activeKey===key && appliedKey===viewKey(card,workflow)) {
      intentId=manual ? id : undefined;committedId=intentId;emit();return true
    }
    const previousIntent=intentId,previousId=currentId
    abort();const request=generation
    controller=new AbortController()
    const signal=controller.signal
    intentId=manual ? id : undefined;pendingId=id;pendingKey=key
    onPause();emit()
    try {
      const profile=cache.get(key) || await requestProfile(card,signal)
      if(disposed || request!==generation || signal.aborted) return false
      const latest=find(id)
      if(!isCurrentRangeCard(latest,workflow,now()) || rangeCardProfileKey(latest)!==key || !manual && first()?.id!==id) {
        restoreDefault('所选方案已更新，请选择当前方案。');return false
      }
      pendingId=undefined;pendingKey=undefined
      apply(latest,profile)
      committedId=manual ? id : undefined;put(key,profile)
      return true
    } catch(reason) {
      if(disposed || request!==generation || signal.aborted) return false
      cache.delete(key)
      pendingId=undefined;pendingKey=undefined
      intentId=isCurrentRangeCard(find(previousIntent),workflow,now()) ? previousIntent : undefined
      currentId=isCurrentRangeCard(find(previousId),workflow,now()) ? previousId : undefined;emit()
      onError(reason.message || '切换方案失败，请重试。')
      if(!intentId && manual) restoreDefault()
      else if(!manual) {currentId=undefined;emit()}
      return false
    }
  }
  return {
    get pinned(){return Boolean(intentId || pendingId)},
    select:id=>select(id,true),
    update(value) {
      if(disposed) return
      if(value?.mode!=='departure_range' || value.stage==='forecast_changed') {this.reset();return}
      const group=groupKey(value),revision=value.selection_revision ?? -1
      if(group!==lastGroup) {this.reset();lastGroup=group}
      else if(revision<lastRevision) return
      lastRevision=revision;workflow=value
      if(intentId) {
        const card=find(intentId)
        if(!isCurrentRangeCard(card,workflow,now())) {restoreDefault('所选方案已更新或移出推荐列表，已恢复当前默认方案。');return}
        const key=rangeCardProfileKey(card)
        if(pendingId===intentId && pendingKey===key) {emit();return}
        if(activeProfile && activeKey===key) {
          if(appliedKey!==viewKey(card,workflow)) {
            try {apply(card,activeProfile)} catch(reason) {restoreDefault(reason.message)}
          } else emit()
        } else void select(intentId,true)
      } else if(pendingId && pendingId!==first()?.id) {
        restoreDefault()
      } else {
        if(!pendingId) currentId=first()?.id
        emit()
      }
    },
    reset() {
      abort();cache.clear();workflow=undefined;intentId=undefined;committedId=undefined;currentId=undefined;lastGroup=undefined;lastRevision=-1
      activeProfile=undefined;activeKey=undefined;appliedKey=undefined;emit()
    },
    cancelPending(){abort();intentId=committedId;cache.clear();emit()},
    dispose(){this.reset();disposed=true},
  }
}
