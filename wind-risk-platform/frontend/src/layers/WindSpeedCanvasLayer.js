import L from 'leaflet'

const COLOR_STOPS = [
  [0, [37, 99, 235]],
  [1.5, [34, 197, 94]],
  [3.3, [250, 204, 21]],
  [5.4, [249, 115, 22]],
  [7.9, [239, 68, 68]],
  [10, [126, 34, 206]],
]

const interpolateColor = (speed) => {
  const value = Math.max(COLOR_STOPS[0][0], Math.min(speed, COLOR_STOPS.at(-1)[0]))
  const upperIndex = COLOR_STOPS.findIndex(([stop]) => value <= stop)
  if (upperIndex <= 0) return `rgb(${COLOR_STOPS[0][1].join(',')})`
  const [lowerValue, lowerColor] = COLOR_STOPS[upperIndex - 1]
  const [upperValue, upperColor] = COLOR_STOPS[upperIndex]
  const ratio = (value - lowerValue) / (upperValue - lowerValue)
  const rgb = lowerColor.map((channel, index) => Math.round(channel + (upperColor[index] - channel) * ratio))
  return `rgb(${rgb.join(',')})`
}

export const createWindSpeedCanvasLayer = (windSpeed, options = {}) => {
  const WindSpeedCanvasLayer = L.Layer.extend({
    options: { pane: 'windHeatPane', opacity: 0.55, filterRanges: null, ...options },

    initialize(data, layerOptions) {
      L.setOptions(this, layerOptions)
      this.windSpeed = data
    },

    onAdd(map) {
      this.map = map
      this.canvas = L.DomUtil.create('canvas', 'wind-speed-canvas-layer')
      this.canvas.style.pointerEvents = 'none'
      this.canvas.style.opacity = this.options.opacity
      map.getPane(this.options.pane).appendChild(this.canvas)
      map.on('moveend zoomend resize', this.redraw, this)
      this.redraw()
    },

    onRemove(map) {
      map.off('moveend zoomend resize', this.redraw, this)
      L.DomUtil.remove(this.canvas)
      this.canvas = null
      this.map = null
    },

    redraw() {
      if (!this.map || !this.canvas || !this.windSpeed?.header) return
      const size = this.map.getSize()
      const ratio = window.devicePixelRatio || 1
      const topLeft = this.map.containerPointToLayerPoint([0, 0])
      L.DomUtil.setPosition(this.canvas, topLeft)
      this.canvas.style.width = `${size.x}px`
      this.canvas.style.height = `${size.y}px`
      this.canvas.width = Math.round(size.x * ratio)
      this.canvas.height = Math.round(size.y * ratio)

      const context = this.canvas.getContext('2d')
      context.setTransform(ratio, 0, 0, ratio, 0, 0)
      context.clearRect(0, 0, size.x, size.y)
      this.drawVisibleCells(context, topLeft)
    },

    drawVisibleCells(context, topLeft) {
      const { nx, ny, lo1, la1, dx, dy } = this.windSpeed.header
      const bounds = this.map.getBounds()
      const westEdge = lo1 - dx / 2
      const northEdge = la1 + dy / 2
      const colStart = Math.max(0, Math.floor((bounds.getWest() - westEdge) / dx))
      const colEnd = Math.min(nx - 1, Math.ceil((bounds.getEast() - westEdge) / dx) - 1)
      const rowStart = Math.max(0, Math.floor((northEdge - bounds.getNorth()) / dy))
      const rowEnd = Math.min(ny - 1, Math.ceil((northEdge - bounds.getSouth()) / dy) - 1)

      for (let row = rowStart; row <= rowEnd; row += 1) {
        const north = northEdge - row * dy
        const south = north - dy
        const y1 = this.map.latLngToLayerPoint([north, lo1]).y - topLeft.y
        const y2 = this.map.latLngToLayerPoint([south, lo1]).y - topLeft.y
        for (let col = colStart; col <= colEnd; col += 1) {
          const i = row * nx + col
          const speed = this.windSpeed.data[i]
          if (!Number.isFinite(speed)) continue
          if (this.options.filterRanges && this.options.filterRanges.length > 0) {
            const inRange = this.options.filterRanges.some(([min, max]) => speed >= min && speed <= max)
            if (!inRange) continue
          }
          const west = westEdge + col * dx
          const east = west + dx
          const x1 = this.map.latLngToLayerPoint([la1, west]).x - topLeft.x
          const x2 = this.map.latLngToLayerPoint([la1, east]).x - topLeft.x
          context.fillStyle = interpolateColor(speed)
          context.fillRect(Math.floor(x1), Math.floor(y1), Math.ceil(x2 - x1) + 1, Math.ceil(y2 - y1) + 1)
        }
      }

      // Second pass: Highlight blocks with large differences
      if (this.options.showMutation && this.options.windVelocity && this.options.thresholds) {
        const [uSource, vSource] = this.options.windVelocity
        if (!uSource || !vSource) return
        const uData = uSource.data
        const vData = vSource.data
        const thresholds = this.options.thresholds
        
        const getLevel = (s) => {
          if (s <= thresholds.safe) return 0
          if (s <= thresholds.notice) return 1
          if (s <= thresholds.warning) return 2
          if (s <= thresholds.danger) return 3
          return 4
        }

        for (let row = rowStart; row <= rowEnd; row += 1) {
          const north = northEdge - row * dy
          const south = north - dy
          const y1 = this.map.latLngToLayerPoint([north, lo1]).y - topLeft.y
          const y2 = this.map.latLngToLayerPoint([south, lo1]).y - topLeft.y
          
          for (let col = colStart; col <= colEnd; col += 1) {
            const i = row * nx + col
            const speed = this.windSpeed.data[i]
            if (!Number.isFinite(speed)) continue

            if (this.options.filterRanges && this.options.filterRanges.length > 0) {
              const inRange = this.options.filterRanges.some(([min, max]) => speed >= min && speed <= max)
              if (!inRange) continue
            }

            const level = getLevel(speed)
            const u1 = uData[i]
            const v1 = vData[i]

            const neighbors = [
              { nr: row - 1, nc: col, edge: 'top' },
              { nr: row + 1, nc: col, edge: 'bottom' },
              { nr: row, nc: col - 1, edge: 'left' },
              { nr: row, nc: col + 1, edge: 'right' }
            ]

            const edgesToDraw = []

            for (const { nr, nc, edge } of neighbors) {
              if (nr >= 0 && nr < ny && nc >= 0 && nc < nx) {
                const ni = nr * nx + nc
                const speed2 = this.windSpeed.data[ni]
                if (!Number.isFinite(speed2)) continue
                
                let isMutation = false
                const level2 = getLevel(speed2)
                const levelDiffThreshold = thresholds.mutationLevelDiff !== undefined ? thresholds.mutationLevelDiff : 3
                if (Math.abs(level - level2) >= levelDiffThreshold) {
                  isMutation = true
                } else {
                  const u2 = uData[ni]
                  const v2 = vData[ni]
                  const angleThreshold = thresholds.mutationAngle !== undefined ? thresholds.mutationAngle : 90
                  const dotProduct = u1 * u2 + v1 * v2
                  const mag1 = Math.hypot(u1, v1)
                  const mag2 = Math.hypot(u2, v2)
                  if (mag1 > 0.1 && mag2 > 0.1) {
                    const cosTheta = Math.max(-1, Math.min(1, dotProduct / (mag1 * mag2)))
                    const angle = Math.acos(cosTheta) * (180 / Math.PI)
                    if (angle >= angleThreshold) {
                      isMutation = true
                    }
                  }
                }

                if (isMutation) {
                  edgesToDraw.push(edge)
                }
              }
            }

            if (edgesToDraw.length > 0) {
              const west = westEdge + col * dx
              const east = west + dx
              const x1 = this.map.latLngToLayerPoint([la1, west]).x - topLeft.x
              const x2 = this.map.latLngToLayerPoint([la1, east]).x - topLeft.x
              
              const rectX = Math.floor(x1)
              const rectY = Math.floor(y1)
              const rectX2 = rectX + Math.ceil(x2 - x1)
              const rectY2 = rectY + Math.ceil(y2 - y1)

              context.save()
              context.strokeStyle = '#00f3ff'
              context.lineWidth = 2.5
              context.shadowColor = '#00f3ff'
              context.shadowBlur = 5
              context.beginPath()
              
              if (edgesToDraw.includes('top')) {
                context.moveTo(rectX, rectY)
                context.lineTo(rectX2, rectY)
              }
              if (edgesToDraw.includes('bottom')) {
                context.moveTo(rectX, rectY2)
                context.lineTo(rectX2, rectY2)
              }
              if (edgesToDraw.includes('left')) {
                context.moveTo(rectX, rectY)
                context.lineTo(rectX, rectY2)
              }
              if (edgesToDraw.includes('right')) {
                context.moveTo(rectX2, rectY)
                context.lineTo(rectX2, rectY2)
              }
              context.stroke()
              context.restore()
            }
          }
        }
      }
    },
  })

  return new WindSpeedCanvasLayer(windSpeed, options)
}
