const TEN_MINUTES = 600000

export const wrfPollDelay = (sync, now = Date.now()) => {
  if (sync?.running) return 10000
  const next = Date.parse(sync?.next_check_at)
  return Number.isFinite(next) ? Math.max(1000, Math.min(TEN_MINUTES, next - now + 1000)) : TEN_MINUTES
}

const timestamp = (option) => Date.parse(option.valid_time?.replace(' UTC', ':00Z').replace(' ', 'T'))

export const chooseWrfTime = (times, selectedLabel, lookahead, previousLookahead, now = Date.now()) => {
  const target = times.find((option) => option.valid_time === lookahead?.target_valid_time)
  if (previousLookahead?.available === false && lookahead?.available && target) return target
  const selected = times.find((option) => option.label === selectedLabel)
  if (selected && timestamp(selected) >= now) return selected
  return target || times.find((option) => timestamp(option) >= now) || times.at(-1)
}
