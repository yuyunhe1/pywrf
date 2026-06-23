import L from 'leaflet'

const COLOR_STOPS = [
  [0, [37, 99, 235]],
  [3, [34, 197, 94]],
  [6, [250, 204, 21]],
  [8, [249, 115, 22]],
  [10, [239, 68, 68]],
  [15, [126, 34, 206]],
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
    options: { pane: 'windHeatPane', opacity: 0.55, ...options },

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
          const speed = this.windSpeed.data[row * nx + col]
          if (!Number.isFinite(speed)) continue
          const west = westEdge + col * dx
          const east = west + dx
          const x1 = this.map.latLngToLayerPoint([la1, west]).x - topLeft.x
          const x2 = this.map.latLngToLayerPoint([la1, east]).x - topLeft.x
          context.fillStyle = interpolateColor(speed)
          context.fillRect(Math.floor(x1), Math.floor(y1), Math.ceil(x2 - x1) + 1, Math.ceil(y2 - y1) + 1)
        }
      }
    },
  })

  return new WindSpeedCanvasLayer(windSpeed, options)
}
