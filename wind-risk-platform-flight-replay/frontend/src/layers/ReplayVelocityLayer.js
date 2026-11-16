import L from 'leaflet'
import 'leaflet-velocity'
import SharedWindy from './SharedWindy.js'

// leaflet-velocity 2.1.4 leaves a zero-delay draw callback and animation frame
// pending after removal. Replay can replace/remove a layer before either runs.
if (L.CanvasLayer && !L.CanvasLayer.prototype.flightReplayLifecycle) {
  const prototype = L.CanvasLayer.prototype
  const overrides = { flightReplayLifecycle: true }
  for (const name of ['_onLayerDidMove', '_onLayerDidResize', 'drawLayer', '_animateZoom']) {
    const method = prototype[name]
    overrides[name] = function (...args) {
      if (!this._map || !this._canvas) return
      return method.apply(this, args)
    }
  }
  const remove = prototype.onRemove
  overrides.onRemove = function (map) {
    if (this._frame) L.Util.cancelAnimFrame(this._frame)
    this._frame = null
    return remove.call(this, map)
  }
  L.CanvasLayer.include(overrides)
}

const ReplayVelocityLayer = L.VelocityLayer.extend({
  onDrawLayer() {
    if (!this._map || !this._canvasLayer?._canvas) return
    if (!this._windy) return this._initWindy(this)
    if (!this.options.data) return
    clearTimeout(this._timer)
    this._timer = setTimeout(() => this._startWindy(), 0)
  },
  _initWindy(self) {
    this._windy = new SharedWindy({ canvas: this._canvasLayer._canvas, map: this._map, ...this.options })
    this._context = this._canvasLayer._canvas.getContext('2d')
    this._canvasLayer._canvas.classList.add('velocity-overlay')
    this.onDrawLayer()
    this._map.on('dragstart zoomstart', this._windy.stop)
    this._replayMove = () => this._clearAndRestart()
    this._replayResize = () => this._clearWind()
    this._map.on('dragend zoomend', this._replayMove)
    this._map.on('resize', this._replayResize)
    this._initMouseHandler(false)
  },
  _startWindy() {
    if (!this._map || !this._windy || !this._canvasLayer?._canvas) return
    return L.VelocityLayer.prototype._startWindy.call(this)
  },
  _clearAndRestart() {
    // Keep the last painted pixels while the replacement field is prepared.
    // SharedWindy cancels the old calculations and clears on its first new draw.
    this._startWindy()
  },
  onRemove(map) {
    if (this._windy) map.off('dragstart zoomstart', this._windy.stop)
    if (this._replayMove) map.off('dragend zoomend', this._replayMove)
    if (this._replayResize) map.off('resize', this._replayResize)
    L.VelocityLayer.prototype.onRemove.call(this, map)
    this._context = null
  },
})

export const createReplayVelocityLayer = (options) => new ReplayVelocityLayer(options)
