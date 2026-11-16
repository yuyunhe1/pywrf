export const HORIZONTAL_WIND_SHEAR_DISPLAY_EPSILON = 0.01

const finiteNumber = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export const formatMaxHorizontalWindShear = (value, blocked = false) => {
  if (blocked) return { value: '超过阈值', unit: '' }
  if (value === null || value === undefined || value === '') {
    return { value: '-', unit: 'm/s' }
  }
  const number = finiteNumber(value)
  if (number === null) return { value: '-', unit: 'm/s' }
  return { value: number.toFixed(2), unit: 'm/s' }
}

export const buildHorizontalWindShearChartModel = (
  profile,
  threshold,
  displayEpsilon = HORIZONTAL_WIND_SHEAR_DISPLAY_EPSILON,
) => {
  const epsilon = finiteNumber(displayEpsilon) ?? HORIZONTAL_WIND_SHEAR_DISPLAY_EPSILON
  const events = Array.isArray(profile)
    ? profile.filter((item) => {
        const shear = finiteNumber(item?.horizontal_wind_shear)
        const center = finiteNumber(item?.center_distance_km)
        return shear !== null && center !== null && shear > epsilon
      })
    : []
  const routeDistanceKm = Array.isArray(profile)
    ? profile.reduce((maximum, item) => {
        const endDistance = finiteNumber(item?.end_distance_km)
        return endDistance === null ? maximum : Math.max(maximum, endDistance)
      }, 0)
    : 0

  return {
    events,
    barData: events.map((item) => ({
      value: [Number(item.center_distance_km), Number(item.horizontal_wind_shear)],
      profile: item,
    })),
    threshold: finiteNumber(threshold),
    routeDistanceKm,
  }
}
