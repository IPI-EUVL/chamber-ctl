import numpy as np
import pytest

from chamber_ctl.interfaces.scope_interface import PhosphorScopeTk


class _Image:
    def __init__(self):
        self.extent = None

    def set_extent(self, extent):
        self.extent = extent

    def set_data(self, _data):
        pass


class _Axis:
    def __init__(self):
        self.limits = None

    def set_xlim(self, tmin, tmax):
        self.limits = (tmin, tmax)


class _Canvas:
    def __init__(self):
        self.draw_count = 0

    def draw_idle(self):
        self.draw_count += 1


class _Master:
    def __init__(self):
        self.cancelled = []
        self.viewable = True

    def after_cancel(self, job):
        self.cancelled.append(job)

    def winfo_viewable(self):
        return self.viewable


def _scope() -> PhosphorScopeTk:
    scope = object.__new__(PhosphorScopeTk)
    scope.master = _Master()
    scope.tmin = 0.0
    scope.tmax = 1.0
    scope.vmin = -1.0
    scope.vmax = 1.0
    scope.h = 2
    scope.w = 2
    scope.buf = np.zeros((2, 2), dtype=np.float32)
    scope.im = _Image()
    scope.ax = _Axis()
    scope.canvas = _Canvas()
    scope.paused = False
    scope.decay = 0.5
    scope._closed = False
    scope._after_job = "job-1"
    return scope


def test_scope_updates_time_limits_and_clears_persistence() -> None:
    scope = _scope()
    scope.buf.fill(1.0)

    scope.set_time_limits((-2e-6, 6e-6))

    assert (scope.tmin, scope.tmax) == (-2e-6, 6e-6)
    assert scope.im.extent == [-2e-6, 6e-6, -1.0, 1.0]
    assert scope.ax.limits == (-2e-6, 6e-6)
    assert not scope.buf.any()
    with pytest.raises(ValueError, match="increasing"):
        scope.set_time_limits((1.0, 1.0))


def test_scope_close_cancels_scheduled_redraw_once() -> None:
    scope = _scope()

    scope.close()
    scope.close()

    assert scope.master.cancelled == ["job-1"]
    assert scope._after_job is None
    assert scope._closed is True


def test_scope_skips_rendering_while_its_tab_is_hidden() -> None:
    scope = _scope()
    scope.buf.fill(1.0)
    scope.master.viewable = False

    scope._tick_render()

    assert np.all(scope.buf == 1.0)
    assert scope.canvas.draw_count == 0

    scope.master.viewable = True
    scope._tick_render()

    assert np.all(scope.buf == 0.5)
    assert scope.canvas.draw_count == 1