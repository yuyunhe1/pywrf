// Beaufort land observations, checked online on 2026-11-05:
// https://www.weather.gov/pqr/wind
// https://www.weather.gov/mfl/beaufort
// Use the current legend interval so edited risk thresholds keep accurate text.
const WIND_FEEL = [
  { max: 0.2, name: '0级 · 静风', description: '几乎感觉不到风，烟柱直上，树叶基本不动。' },
  { max: 1.5, name: '1级 · 软风', description: '风很轻，烟随风略微飘移，风向标通常还不转动。' },
  { max: 3.3, name: '2级 · 轻风', description: '脸上能感觉到风，树叶轻响，风向标开始转动。' },
  { max: 5.4, name: '3级 · 微风', description: '树叶和细枝持续摆动，轻旗被风展开。' },
  { max: 7.9, name: '4级 · 和风', description: '小树枝摇动，地面灰尘和零散纸片被风吹起。' },
  { max: 10.7, name: '5级 · 清劲风', description: '有叶的小树开始摇摆，内陆水面出现带白沫的小浪。' },
  { max: 13.8, name: '6级 · 强风', description: '大树枝摆动，电线附近可听到风声，撑伞开始吃力。' },
  { max: 17.1, name: '7级 · 疾风', description: '整棵树摇动，迎风步行明显吃力。' },
  { max: Infinity, name: '8级及以上 · 大风及更强', description: '树枝可能折断，行走困难；风力继续增强时，可能造成树木和建筑物损坏。' },
]

export const windFeelForRange = (min, max) => WIND_FEEL.filter((level, index) =>
  level.max > min && (index ? WIND_FEEL[index - 1].max : 0) < max)
