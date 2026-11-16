export const WIND_BAND_COLORS = ['#00b84b', '#e6a100', '#e66b00', '#c21e1f', '#6a1b9a']

export const windBandIndex = (speed, thresholds) => {
  const upper = [thresholds.safe, thresholds.notice, thresholds.warning, thresholds.danger]
  const index = upper.findIndex((limit) => speed <= limit)
  return index < 0 ? 4 : index
}
