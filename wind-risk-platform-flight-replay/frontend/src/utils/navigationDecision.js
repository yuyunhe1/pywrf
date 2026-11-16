const finiteNumber = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export const hasHorizontalWindShearRisk = (analysis) => Boolean(
  analysis?.horizontal_wind_shear_risk
  || analysis?.wind_shear_fallback
  || analysis?.wind_shear_failure
  || analysis?.wind_shear?.highest_shear_level === '禁飞'
  || Number(analysis?.wind_shear?.horizontal_shear_warning_count || 0) > 0
)

export const buildNavigationDecision = (analysis, dangerThreshold = 7.9) => {
  if (!analysis) {
    return {
      level: '待评估',
      color: '#00f3ff',
      message: '生成规划航线后，系统会根据风速和水平风切变硬约束给出通航结论。',
    }
  }

  const hardLimit = finiteNumber(dangerThreshold) ?? 7.9
  const maxWindSpeed = finiteNumber(analysis.max_wind_speed)
  const shearRisk = hasHorizontalWindShearRisk(analysis)
  const prohibited = Boolean(
    analysis.navigation_allowed === false
    || analysis.navigation_decision === '禁止通航'
    || analysis.endpoint_hard_block
    || shearRisk
    || (maxWindSpeed !== null && maxWindSpeed >= hardLimit)
  )

  if (prohibited) {
    return {
      level: '禁止通航',
      color: '#ea5455',
      message: analysis.navigation_decision_reason
        || analysis.endpoint_hard_block?.message
        || analysis.wind_shear_fallback?.message
        || analysis.wind_shear_failure?.message
        || (shearRisk
          ? '存在达到硬约束阈值的水平风切变，禁止通航。'
          : `风速达到或超过 ${hardLimit} m/s 硬上限，禁止通航。`),
    }
  }

  return {
    level: '允许通航',
    color: '#28c76f',
    message: analysis.navigation_decision_reason
      || '航线未触发风速或水平风切变硬约束，允许通航。',
  }
}
