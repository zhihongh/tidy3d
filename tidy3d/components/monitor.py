"""Objects that define how data is recorded from simulation."""
from abc import ABC, abstractmethod
from typing import Union, Tuple

import pydantic.v1 as pydantic
import numpy as np

from .types import Ax, EMField, ArrayFloat1D, FreqArray, FreqBound, Bound, Size
from .types import Literal, Direction, Coordinate, Axis, ObsGridArray, BoxSurface
from .validators import assert_plane, validate_freqs_not_empty, validate_freqs_min
from .base import cached_property, Tidy3dBaseModel, skip_if_fields_missing
from .mode import ModeSpec
from .apodization import ApodizationSpec
from .medium import MediumType
from .viz import ARROW_COLOR_MONITOR, ARROW_ALPHA
from ..constants import HERTZ, SECOND, MICROMETER, RADIAN, inf
from ..exceptions import SetupError, ValidationError
from ..log import log

from .base_sim.monitor import AbstractMonitor


BYTES_REAL = 4
BYTES_COMPLEX = 8
WARN_NUM_FREQS = 2000
WARN_NUM_MODES = 100

# Field projection windowing factor that determines field decay at the edges of surface field
# projection monitors. A value of 15 leads to a decay of < 1e-3x in field amplitude.
# This number relates directly to the standard deviation of the Gaussian function which is used
# for windowing the monitor.
WINDOW_FACTOR = 15


class Monitor(AbstractMonitor):
    """Abstract base class for monitors."""

    interval_space: Tuple[Literal[1], Literal[1], Literal[1]] = pydantic.Field(
        (1, 1, 1),
        title="Spatial Interval",
        description="Number of grid step intervals between monitor recordings. If equal to 1, "
        "there will be no downsampling. If greater than 1, the step will be applied, but the "
        "first and last point of the monitor grid are always included. "
        "Not all monitors support values different from 1.",
    )

    colocate: Literal[True] = pydantic.Field(
        True,
        title="Colocate Fields",
        description="Defines whether fields are colocated to grid cell boundaries (i.e. to the "
        "primal grid) on-the-fly during a solver run. Can be toggled for field recording monitors "
        "and is hard-coded for other monitors depending on their specific function.",
    )

    @abstractmethod
    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""

    def _storage_size_solver(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of intermediate data recorded by the monitor during a solver run."""
        return self.storage_size(num_cells=num_cells, tmesh=tmesh)


class FreqMonitor(Monitor, ABC):
    """:class:`Monitor` that records data in the frequency-domain."""

    freqs: FreqArray = pydantic.Field(
        ...,
        title="Frequencies",
        description="Array or list of frequencies stored by the field monitor.",
        units=HERTZ,
    )

    apodization: ApodizationSpec = pydantic.Field(
        ApodizationSpec(),
        title="Apodization Specification",
        description="Sets parameters of (optional) apodization. Apodization applies a windowing "
        "function to the Fourier transform of the time-domain fields into frequency-domain ones, "
        "and can be used to truncate the beginning and/or end of the time signal, for example "
        "to eliminate the source pulse when studying the eigenmodes of a system. Note: apodization "
        "affects the normalization of the frequency-domain fields.",
    )

    _freqs_not_empty = validate_freqs_not_empty()
    _freqs_lower_bound = validate_freqs_min()

    @pydantic.validator("freqs", always=True)
    def _warn_num_freqs(cls, val, values):
        """Warn if number of frequencies is too large."""
        if len(val) > WARN_NUM_FREQS:
            log.warning(
                f"A large number ({len(val)}) of frequencies detected in monitor "
                f"'{values['name']}'. This can lead to solver slow-down and increased cost. "
                "Consider decreasing the number of frequencies in the monitor. This may become a "
                "hard limit in future Tidy3D versions.",
                custom_loc=["freqs"],
            )
        return val

    @cached_property
    def frequency_range(self) -> FreqBound:
        """Frequency range of the array ``self.freqs``.

        Returns
        -------
        Tuple[float, float]
            Minimum and maximum frequencies of the frequency array.
        """
        return (min(self.freqs), max(self.freqs))


class TimeMonitor(Monitor, ABC):
    """:class:`Monitor` that records data in the time-domain."""

    start: pydantic.NonNegativeFloat = pydantic.Field(
        0.0,
        title="Start Time",
        description="Time at which to start monitor recording.",
        units=SECOND,
    )

    stop: pydantic.NonNegativeFloat = pydantic.Field(
        None,
        title="Stop Time",
        description="Time at which to stop monitor recording.  "
        "If not specified, record until end of simulation.",
        units=SECOND,
    )

    interval: pydantic.PositiveInt = pydantic.Field(
        None,
        title="Time Interval",
        description="Sampling rate of the monitor: number of time steps between each measurement. "
        "Set ``inverval`` to 1 for the highest possible resolution in time. "
        "Higher integer values downsample the data by measuring every ``interval`` time steps. "
        "This can be useful for reducing data storage as needed by the application.",
    )

    @pydantic.validator("interval", always=True)
    @skip_if_fields_missing(["start", "stop"])
    def _warn_interval_default(cls, val, values):
        """If all defaults used for time sampler, warn and set ``interval=1`` internally."""

        if val is None:
            start = values.get("start")
            stop = values.get("stop")
            if start == 0.0 and stop is None:
                log.warning(
                    "The monitor 'interval' field was left as its default value, "
                    "which will set it to 1 internally. "
                    "A value of 1 means that the data will be sampled at every time step, "
                    "which may potentially produce more data than desired, "
                    "depending on the use case. "
                    "To reduce data storage, one may downsample the data by setting 'interval > 1'"
                    " or by choosing alternative 'start' and 'stop' values for the time sampling. "
                    "If you intended to use the highest resolution time sampling, "
                    "you may suppress this warning "
                    "by explicitly setting 'interval=1' in the monitor."
                )

            # set 'interval = 1' for backwards compatibility
            val = 1

        return val

    @pydantic.validator("stop", always=True, allow_reuse=True)
    @skip_if_fields_missing(["start"])
    def stop_greater_than_start(cls, val, values):
        """Ensure sure stop is greater than or equal to start."""
        start = values.get("start")
        if val and val < start:
            raise SetupError("Monitor start time is greater than stop time.")
        return val

    def time_inds(self, tmesh: ArrayFloat1D) -> Tuple[int, int]:
        """Compute the starting and stopping index of the monitor in a given discrete time mesh."""

        tmesh = np.array(tmesh)
        tind_beg, tind_end = (0, 0)

        if tmesh.size == 0:
            return (tind_beg, tind_end)

        # If monitor.stop is None, record until the end
        t_stop = self.stop
        if t_stop is None:
            tind_end = int(tmesh.size)
            t_stop = tmesh[-1]
        else:
            tend = np.nonzero(tmesh <= t_stop)[0]
            if tend.size > 0:
                tind_end = int(tend[-1] + 1)

        # Step to compare to in order to handle t_start = t_stop
        dt = 1e-20 if np.array(tmesh).size < 2 else tmesh[1] - tmesh[0]
        # If equal start and stopping time, record one time step
        if np.abs(self.start - t_stop) < dt and self.start <= tmesh[-1]:
            tind_beg = max(tind_end - 1, 0)
        else:
            tbeg = np.nonzero(tmesh[:tind_end] >= self.start)[0]
            tind_beg = tbeg[0] if tbeg.size > 0 else tind_end
        return (tind_beg, tind_end)

    def num_steps(self, tmesh: ArrayFloat1D) -> int:
        """Compute number of time steps for a time monitor."""

        tind_beg, tind_end = self.time_inds(tmesh)
        return int((tind_end - tind_beg) / self.interval)


class AbstractFieldMonitor(Monitor, ABC):
    """:class:`Monitor` that records electromagnetic field data as a function of x,y,z."""

    fields: Tuple[EMField, ...] = pydantic.Field(
        ["Ex", "Ey", "Ez", "Hx", "Hy", "Hz"],
        title="Field Components",
        description="Collection of field components to store in the monitor.",
    )

    interval_space: Tuple[
        pydantic.PositiveInt, pydantic.PositiveInt, pydantic.PositiveInt
    ] = pydantic.Field(
        (1, 1, 1),
        title="Spatial Interval",
        description="Number of grid step intervals between monitor recordings. If equal to 1, "
        "there will be no downsampling. If greater than 1, the step will be applied, but the "
        "first and last point of the monitor grid are always included.",
    )

    colocate: bool = pydantic.Field(
        True,
        title="Colocate Fields",
        description="Toggle whether fields should be colocated to grid cell boundaries (i.e. "
        "primal grid nodes).",
    )


class PlanarMonitor(Monitor, ABC):
    """:class:`Monitor` that has a planar geometry."""

    _plane_validator = assert_plane()

    @cached_property
    def normal_axis(self) -> Axis:
        """Axis normal to the monitor's plane."""
        return self.size.index(0.0)


class AbstractModeMonitor(PlanarMonitor, FreqMonitor):
    """:class:`Monitor` that records mode-related data."""

    mode_spec: ModeSpec = pydantic.Field(
        ...,
        title="Mode Specification",
        description="Parameters to feed to mode solver which determine modes measured by monitor.",
    )

    def plot(
        self,
        x: float = None,
        y: float = None,
        z: float = None,
        ax: Ax = None,
        **patch_kwargs,
    ) -> Ax:
        """Plot this monitor."""
        # call the monitor.plot() function first
        ax = super().plot(x=x, y=y, z=z, ax=ax, **patch_kwargs)

        kwargs_alpha = patch_kwargs.get("alpha")
        arrow_alpha = ARROW_ALPHA if kwargs_alpha is None else kwargs_alpha

        # and then add an arrow using the direction comuputed from `_dir_arrow`.
        ax = self._plot_arrow(
            x=x,
            y=y,
            z=z,
            ax=ax,
            direction=self._dir_arrow,
            bend_radius=self.mode_spec.bend_radius,
            bend_axis=self._bend_axis,
            color=ARROW_COLOR_MONITOR,
            alpha=arrow_alpha,
            both_dirs=True,
        )
        return ax

    @cached_property
    def _dir_arrow(self) -> Tuple[float, float, float]:
        """Source direction normal vector in cartesian coordinates."""
        dx = np.cos(self.mode_spec.angle_phi) * np.sin(self.mode_spec.angle_theta)
        dy = np.sin(self.mode_spec.angle_phi) * np.sin(self.mode_spec.angle_theta)
        dz = np.cos(self.mode_spec.angle_theta)
        return self.unpop_axis(dz, (dx, dy), axis=self.normal_axis)

    @cached_property
    def _bend_axis(self) -> Axis:
        if self.mode_spec.bend_radius is None:
            return None
        in_plane = [0, 0]
        in_plane[self.mode_spec.bend_axis] = 1
        direction = self.unpop_axis(0, in_plane, axis=self.normal_axis)
        return direction.index(1)

    @pydantic.validator("mode_spec", always=True)
    def _warn_num_modes(cls, val, values):
        """Warn if number of modes is too large."""
        if val.num_modes > WARN_NUM_MODES:
            log.warning(
                f"A large number ({val.num_modes}) of modes requested in monitor "
                f"'{values['name']}'. This can lead to solver slow-down and increased cost. "
                "Consider decreasing the number of modes and using 'ModeSpec.target_neff' "
                "to target the modes of interest. This may become a hard limit in future "
                "Tidy3D versions.",
                custom_loc=["mode_spec", "num_modes"],
            )
        return val

    def _storage_size_solver(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of intermediate data recorded by the monitor during a solver run."""
        # Need to store all fields on the mode surface
        bytes_single = BYTES_COMPLEX * num_cells * len(self.freqs) * self.mode_spec.num_modes * 6
        if self.mode_spec.precision == "double":
            return 2 * bytes_single
        return bytes_single


class FieldMonitor(AbstractFieldMonitor, FreqMonitor):
    """:class:`Monitor` that records electromagnetic fields in the frequency domain.

    Example
    -------
    >>> monitor = FieldMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,2),
    ...     fields=['Hx'],
    ...     freqs=[250e12, 300e12],
    ...     name='steady_state_monitor',
    ...     colocate=True)
    """

    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # stores 1 complex number per grid cell, per frequency, per field
        return BYTES_COMPLEX * num_cells * len(self.freqs) * len(self.fields)


class FieldTimeMonitor(AbstractFieldMonitor, TimeMonitor):
    """:class:`Monitor` that records electromagnetic fields in the time domain.

    Example
    -------
    >>> monitor = FieldTimeMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,2),
    ...     fields=['Hx'],
    ...     start=1e-13,
    ...     stop=5e-13,
    ...     interval=2,
    ...     colocate=True,
    ...     name='movie_monitor')
    """

    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # stores 1 real number per grid cell, per time step, per field
        num_steps = self.num_steps(tmesh)
        return BYTES_REAL * num_steps * num_cells * len(self.fields)


class PermittivityMonitor(FreqMonitor):
    """:class:`Monitor` that records the diagonal components of the complex-valued relative
    permittivity tensor in the frequency domain. The recorded data has the same shape as a
    :class:`.FieldMonitor` of the same geometry: the permittivity values are saved at the
    Yee grid locations, and can be interpolated to any point inside the monitor.

    Note
    ----
    If 2D materials are present, then the permittivity values correspond to the
    volumetric equivalent of the 2D materials.

    Example
    -------
    >>> monitor = PermittivityMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,2),
    ...     freqs=[250e12, 300e12],
    ...     name='eps_monitor')
    """

    colocate: Literal[False] = pydantic.Field(
        False,
        title="Colocate Fields",
        description="Colocation turned off, since colocated permittivity values do not have a "
        "physical meaning - they do not correspond to the subpixel-averaged ones.",
    )

    interval_space: Tuple[
        pydantic.PositiveInt, pydantic.PositiveInt, pydantic.PositiveInt
    ] = pydantic.Field(
        (1, 1, 1),
        title="Spatial Interval",
        description="Number of grid step intervals between monitor recordings. If equal to 1, "
        "there will be no downsampling. If greater than 1, the step will be applied, but the "
        "first and last point of the monitor grid are always included.",
    )

    apodization: ApodizationSpec = pydantic.Field(
        ApodizationSpec(),
        title="Apodization Specification",
        description="This field is ignored in this monitor.",
    )

    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # stores 3 complex number per grid cell, per frequency
        return BYTES_COMPLEX * num_cells * len(self.freqs) * 3


class SurfaceIntegrationMonitor(Monitor, ABC):
    """Abstract class for monitors that perform surface integrals during the solver run, as in
    flux and near to far transformations."""

    normal_dir: Direction = pydantic.Field(
        None,
        title="Normal Vector Orientation",
        description="Direction of the surface monitor's normal vector w.r.t. "
        "the positive x, y or z unit vectors. Must be one of ``'+'`` or ``'-'``. "
        "Applies to surface monitors only, and defaults to ``'+'`` if not provided.",
    )

    exclude_surfaces: Tuple[BoxSurface, ...] = pydantic.Field(
        None,
        title="Excluded Surfaces",
        description="Surfaces to exclude in the integration, if a volume monitor.",
    )

    @property
    def integration_surfaces(self):
        """Surfaces of the monitor where fields will be recorded for subsequent integration."""
        if self.size.count(0.0) == 0:
            return self.surfaces_with_exclusion(**self.dict())
        return [self]

    @pydantic.root_validator(skip_on_failure=True)
    def normal_dir_exists_for_surface(cls, values):
        """If the monitor is a surface, set default ``normal_dir`` if not provided.
        If the monitor is a box, warn that ``normal_dir`` is relevant only for surfaces."""
        normal_dir = values.get("normal_dir")
        name = values.get("name")
        size = values.get("size")
        if size.count(0.0) != 1:
            if normal_dir is not None:
                log.warning(
                    "The ``normal_dir`` field is relevant only for surface monitors "
                    f"and will be ignored for monitor {name}, which is a box."
                )
        else:
            if normal_dir is None:
                values["normal_dir"] = "+"
        return values

    @pydantic.root_validator(skip_on_failure=True)
    def check_excluded_surfaces(cls, values):
        """Error if ``exclude_surfaces`` is provided for a surface monitor."""
        exclude_surfaces = values.get("exclude_surfaces")
        if exclude_surfaces is None:
            return values
        name = values.get("name")
        size = values.get("size")
        if size.count(0.0) > 0:
            raise SetupError(
                f"Can't specify ``exclude_surfaces`` for surface monitor {name}; "
                "valid for box monitors only."
            )
        return values

    def _storage_size_solver(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of intermediate data recorded by the monitor during a solver run."""
        # Need to store all fields on the integration surface. Frequency-domain monitors store at
        # all frequencies, time domain at the current time step only.
        num_sample = len(getattr(self, "freqs", [0]))
        return BYTES_COMPLEX * num_cells * num_sample * 6


class AbstractFluxMonitor(SurfaceIntegrationMonitor, ABC):
    """:class:`Monitor` that records flux during the solver run."""


class FluxMonitor(AbstractFluxMonitor, FreqMonitor):
    """:class:`Monitor` that records power flux in the frequency domain.
    If the monitor geometry is a 2D box, the total flux through this plane is returned, with a
    positive sign corresponding to power flow in the positive direction along the axis normal to
    the plane. If the geometry is a 3D box, the total power coming out of the box is returned by
    integrating the flux over all box surfaces (excpet the ones defined in ``exclude_surfaces``).

    Example
    -------
    >>> monitor = FluxMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,0),
    ...     freqs=[200e12, 210e12],
    ...     name='flux_monitor')
    """

    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # stores 1 real number per frequency
        return BYTES_REAL * len(self.freqs)


class FluxTimeMonitor(AbstractFluxMonitor, TimeMonitor):
    """:class:`Monitor` that records power flux in the time domain.
    If the monitor geometry is a 2D box, the total flux through this plane is returned, with a
    positive sign corresponding to power flow in the positive direction along the axis normal to
    the plane. If the geometry is a 3D box, the total power coming out of the box is returned by
    integrating the flux over all box surfaces (excpet the ones defined in ``exclude_surfaces``).

    Example
    -------
    >>> monitor = FluxTimeMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,0),
    ...     start=1e-13,
    ...     stop=5e-13,
    ...     interval=2,
    ...     name='flux_vs_time')
    """

    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # stores 1 real number per time step
        num_steps = self.num_steps(tmesh)
        return BYTES_REAL * num_steps


class ModeMonitor(AbstractModeMonitor):
    """:class:`Monitor` that records amplitudes from modal decomposition of fields on plane.
    The amplitudes are defined as
    ``mode_solver_data.dot(recorded_field) / mode_solver_data.dot(mode_solver_data)``, where
    ``recorded_field`` is the field data recorded in the FDTD simulation at the monitor frequencies,
    and ``mode_solver_data`` is the mode data from the mode solver at the monitor plane.
    This gives the power amplitude of ``recorded_field`` carried by each mode.

    Example
    -------
    >>> mode_spec = ModeSpec(num_modes=3)
    >>> monitor = ModeMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,0),
    ...     freqs=[200e12, 210e12],
    ...     mode_spec=mode_spec,
    ...     name='mode_monitor')
    """

    colocate: Literal[False] = pydantic.Field(
        False,
        title="Colocate Fields",
        description="Defines whether fields are colocated to grid cell boundaries (i.e. to the "
        "primal grid) on-the-fly during a solver run. Can be toggled for field recording monitors "
        "and is hard-coded for other monitors depending on their specific function.",
    )

    def storage_size(self, num_cells: int, tmesh: int) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # stores 3 complex numbers per frequency, per mode.
        return 3 * BYTES_COMPLEX * len(self.freqs) * self.mode_spec.num_modes


class ModeSolverMonitor(AbstractModeMonitor):
    """:class:`Monitor` that stores the mode field profiles returned by the mode solver in the
    monitor plane.

    Example
    -------
    >>> mode_spec = ModeSpec(num_modes=3)
    >>> monitor = ModeSolverMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,0),
    ...     freqs=[200e12, 210e12],
    ...     mode_spec=mode_spec,
    ...     name='mode_monitor')
    """

    direction: Direction = pydantic.Field(
        "+",
        title="Propagation Direction",
        description="Direction of waveguide mode propagation along the axis defined by its normal "
        "dimension.",
    )

    colocate: bool = pydantic.Field(
        True,
        title="Colocate Fields",
        description="Toggle whether fields should be colocated to grid cell boundaries (i.e. "
        "primal grid nodes).",
    )

    def storage_size(self, num_cells: int, tmesh: int) -> int:
        """Size of monitor storage given the number of points after discretization."""
        bytes_single = 6 * BYTES_COMPLEX * num_cells * len(self.freqs) * self.mode_spec.num_modes
        if self.mode_spec.precision == "double":
            return 2 * bytes_single
        return bytes_single


class FieldProjectionSurface(Tidy3dBaseModel):
    """Data structure to store surface monitors where near fields are recorded for
    field projections."""

    monitor: FieldMonitor = pydantic.Field(
        ...,
        title="Field Monitor",
        description=":class:`.FieldMonitor` on which near fields will be sampled and integrated.",
    )

    normal_dir: Direction = pydantic.Field(
        ...,
        title="Normal Vector Orientation",
        description=":class:`.Direction` of the surface monitor's normal vector w.r.t.\
 the positive x, y or z unit vectors. Must be one of '+' or '-'.",
    )

    @cached_property
    def axis(self) -> Axis:
        """Returns the :class:`.Axis` normal to this surface."""
        # assume that the monitor's axis is in the direction where the monitor is thinnest
        return self.monitor.size.index(0.0)

    @pydantic.validator("monitor", always=True)
    def is_plane(cls, val):
        """Ensures that the monitor is a plane, i.e., its `size` attribute has exactly 1 zero"""
        size = val.size
        if size.count(0.0) != 1:
            raise ValidationError(f"Monitor '{val.name}' must be planar, given size={size}")
        return val


class AbstractFieldProjectionMonitor(SurfaceIntegrationMonitor, FreqMonitor):
    """:class:`Monitor` that samples electromagnetic near fields in the frequency domain
    and projects them to a given set of observation points.
    """

    custom_origin: Coordinate = pydantic.Field(
        None,
        title="Local Origin",
        description="Local origin used for defining observation points. If ``None``, uses the "
        "monitor's center.",
        units=MICROMETER,
    )

    far_field_approx: bool = pydantic.Field(
        True,
        title="Far Field Approximation",
        description="Whether to enable the far field approximation when projecting fields. "
        "If ``True``, terms that decay as O(1/r^2) are ignored, as are the radial components "
        "of fields. Typically, this should be set to ``True`` only when the projection distance "
        "is much larger than the size of the device being modeled, and the projected points are "
        "in the far field of the device.",
    )

    interval_space: Tuple[
        pydantic.PositiveInt, pydantic.PositiveInt, pydantic.PositiveInt
    ] = pydantic.Field(
        (1, 1, 1),
        title="Spatial Interval",
        description="Number of grid step intervals at which near fields are recorded for "
        "projection to the far field, along each direction. If equal to 1, there will be no "
        "downsampling. If greater than 1, the step will be applied, but the first and last "
        "point of the monitor grid are always included. Using values greater than 1 can "
        "help speed up server-side far field projections with minimal accuracy loss, "
        "especially in cases where it is necessary for the grid resolution to be high for "
        "the FDTD simulation, but such a high resolution is unnecessary for the purpose of "
        "projecting the recorded near fields to the far field.",
    )

    window_size: Tuple[pydantic.NonNegativeFloat, pydantic.NonNegativeFloat] = pydantic.Field(
        (0, 0),
        title="Spatial filtering window size",
        description="Size of the transition region of the windowing function used to ensure that "
        "the recorded near fields decay to zero near the edges of the monitor. "
        "The two components refer to the two tangential directions associated with each surface. "
        "For surfaces with the normal along ``x``, the two components are (``y``, ``z``). "
        "For surfaces with the normal along ``y``, the two components are (``x``, ``z``). "
        "For surfaces with the normal along ``z``, the two components are (``x``, ``y``). "
        "Each value must be between 0 and 1, inclusive, and denotes the size of the transition "
        "region over which fields are scaled to less than a thousandth of the original amplitude, "
        "relative to half the size of the monitor in that direction. A value of 0 turns windowing "
        "off in that direction, while a value of 1 indicates that the window will be applied to "
        "the entire monitor in that direction. This field is applicable for surface monitors only, "
        "and otherwise must remain (0, 0).",
    )

    medium: MediumType = pydantic.Field(
        None,
        title="Projection medium",
        description="Medium through which to project fields. Generally, the fields should be "
        "projected through the same medium as the one in which this monitor is placed, and "
        "this is the default behavior when ``medium=None``. A custom ``medium`` can be useful "
        "in some situations for advanced users, but we recommend trying to avoid using a "
        "non-default ``medium``.",
    )

    @pydantic.validator("window_size", always=True)
    @skip_if_fields_missing(["size", "name"])
    def window_size_for_surface(cls, val, values):
        """Ensures that windowing is applied for surface monitors only."""
        size = values.get("size")
        name = values.get("name")

        if size.count(0.0) != 1:
            if val != (0, 0):
                raise ValidationError(
                    f"A non-zero 'window_size' cannot be used for projection monitor '{name}'. "
                    "Windowing can be applied only for surface projection monitors."
                )
        return val

    @pydantic.validator("window_size", always=True)
    @skip_if_fields_missing(["name"])
    def window_size_leq_one(cls, val, values):
        """Ensures that each component of the window size is less than or equal to 1."""
        name = values.get("name")
        if val[0] > 1 or val[1] > 1:
            raise ValidationError(
                f"Each component of 'window_size' for monitor '{name}' "
                "must be less than or equal to 1."
            )
        return val

    @property
    def projection_surfaces(self) -> Tuple[FieldProjectionSurface, ...]:
        """Surfaces of the monitor where near fields will be recorded for subsequent projection."""
        surfaces = self.integration_surfaces
        return [
            FieldProjectionSurface(
                monitor=FieldMonitor(
                    center=surface.center,
                    size=surface.size,
                    freqs=self.freqs,
                    name=surface.name,
                    colocate=True,
                ),
                normal_dir=surface.normal_dir,
            )
            for surface in surfaces
        ]

    @property
    def local_origin(self) -> Coordinate:
        """Returns the local origin associated with this monitor."""
        if self.custom_origin is None:
            return self.center
        return self.custom_origin

    def window_parameters(self, custom_bounds: Bound = None) -> Tuple[Size, Coordinate, Coordinate]:
        """Return the physical size of the window transition region based on the monitor's size
        and optional custom bounds (useful in case the monitor has infinite dimensions). The window
        size is returned in 3D. Also returns the coordinate where the transition region beings on
        the minus and plus side of the monitor."""

        window_size = [0, 0, 0]
        window_minus = [0, 0, 0]
        window_plus = [0, 0, 0]

        # windowing is for surface monitors only
        if self.size.count(0.0) != 1:
            return window_size, window_minus, window_plus

        _, plane_inds = self.pop_axis([0, 1, 2], axis=self.size.index(0.0))

        for i, ind in enumerate(plane_inds):
            if custom_bounds:
                size = min(self.size[ind], custom_bounds[1][ind] - custom_bounds[0][ind])
                bound_min = max(self.bounds[0][ind], custom_bounds[0][ind])
                bound_max = min(self.bounds[1][ind], custom_bounds[1][ind])
            else:
                size = self.size[ind]
                bound_min = self.bounds[0][ind]
                bound_max = self.bounds[1][ind]

            window_size[ind] = self.window_size[i] * size / 2
            window_minus[ind] = bound_min + window_size[ind]
            window_plus[ind] = bound_max - window_size[ind]

        return window_size, window_minus, window_plus

    @staticmethod
    def window_function(
        points: ArrayFloat1D,
        window_size: Size,
        window_minus: Coordinate,
        window_plus: Coordinate,
        dim: int,
    ) -> ArrayFloat1D:
        """Get the windowing function along a given direction for a given set of points."""
        rising_window = np.exp(
            -0.5
            * WINDOW_FACTOR
            * ((points[points < window_minus[dim]] - window_minus[dim]) / window_size[dim]) ** 2
        )
        falling_window = np.exp(
            -0.5
            * WINDOW_FACTOR
            * ((points[points > window_plus[dim]] - window_plus[dim]) / window_size[dim]) ** 2
        )
        window_fn = np.ones_like(points)
        window_fn[points < window_minus[dim]] = rising_window
        window_fn[points > window_plus[dim]] = falling_window
        return window_fn


class FieldProjectionAngleMonitor(AbstractFieldProjectionMonitor):
    """:class:`Monitor` that samples electromagnetic near fields in the frequency domain
    and projects them at given observation angles. The ``center`` and ``size`` fields define
    where the monitor will be placed in order to record near fields, typically very close
    to the structure of interest. The near fields are then projected
    to far-field locations defined by ``phi``, ``theta``, and ``proj_distance``, relative
    to the ``custom_origin``. If the distance between the near and far field locations is
    much larger than the size of the device, one can typically set ``far_field_approx`` to
    ``True``, which will make use of the far-field approximation to speed up calculations.
    If the projection distance is comparable to the size of the device, we recommend setting
    ``far_field_approx`` to ``False``, so that the approximations are not used, and the
    projection is accurate even just a few wavelengths away from the near field locations.
    For applications where the monitor is an open surface rather than a box that
    encloses the device, it is advisable to pick the size of the monitor such that the
    recorded near fields decay to negligible values near the edges of the monitor.

    Example
    -------
    >>> monitor = FieldProjectionAngleMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,2),
    ...     freqs=[250e12, 300e12],
    ...     name='n2f_monitor',
    ...     custom_origin=(1,2,3),
    ...     phi=[0, np.pi/2],
    ...     theta=np.linspace(-np.pi/2, np.pi/2, 100)
    ...     )
    """

    proj_distance: float = pydantic.Field(
        1e6,
        title="Projection Distance",
        description="Radial distance of the projection points from ``local_origin``.",
        units=MICROMETER,
    )

    theta: ObsGridArray = pydantic.Field(
        ...,
        title="Polar Angles",
        description="Polar angles with respect to the global z axis, relative to the location of "
        "``local_origin``, at which to project fields.",
        units=RADIAN,
    )

    phi: ObsGridArray = pydantic.Field(
        ...,
        title="Azimuth Angles",
        description="Azimuth angles with respect to the global z axis, relative to the location of "
        "``local_origin``, at which to project fields.",
        units=RADIAN,
    )

    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # stores 1 complex number per pair of angles, per frequency,
        # for Er, Etheta, Ephi, Hr, Htheta, and Hphi (6 components)
        return BYTES_COMPLEX * len(self.theta) * len(self.phi) * len(self.freqs) * 6


class FieldProjectionCartesianMonitor(AbstractFieldProjectionMonitor):
    """:class:`Monitor` that samples electromagnetic near fields in the frequency domain
    and projects them on a Cartesian observation plane. The ``center`` and ``size`` fields define
    where the monitor will be placed in order to record near fields, typically very close
    to the structure of interest. The near fields are then projected
    to far-field locations defined by ``x``, ``y``, and ``proj_distance``, relative
    to the ``custom_origin``. Here, ``x`` and ``y`` correspond to a local coordinate system
    where the local z axis is defined by ``proj_axis``: which is the axis normal to this monitor.
    If the distance between the near and far field locations is much larger than the size of the
    device, one can typically set ``far_field_approx`` to ``True``, which will make use of the
    far-field approximation to speed up calculations. If the projection distance is comparable
    to the size of the device, we recommend setting ``far_field_approx`` to ``False``,
    so that the approximations are not used, and the projection is accurate even just a few
    wavelengths away from the near field locations.
    For applications where the monitor is an open surface rather than a box that
    encloses the device, it is advisable to pick the size of the monitor such that the
    recorded near fields decay to negligible values near the edges of the monitor.

    Example
    -------
    >>> monitor = FieldProjectionCartesianMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,2),
    ...     freqs=[250e12, 300e12],
    ...     name='n2f_monitor',
    ...     custom_origin=(1,2,3),
    ...     x=[-1, 0, 1],
    ...     y=[-2, -1, 0, 1, 2],
    ...     proj_axis=2,
    ...     proj_distance=5
    ...     )
    """

    proj_axis: Axis = pydantic.Field(
        ...,
        title="Projection Plane Axis",
        description="Axis along which the observation plane is oriented.",
    )

    proj_distance: float = pydantic.Field(
        1e6,
        title="Projection Distance",
        description="Signed distance of the projection plane along ``proj_axis``. "
        "from the plane containing ``local_origin``.",
        units=MICROMETER,
    )

    x: ObsGridArray = pydantic.Field(
        ...,
        title="Local x Observation Coordinates",
        description="Local x observation coordinates w.r.t. ``local_origin`` and ``proj_axis``. "
        "When ``proj_axis`` is 0, this corresponds to the global y axis. "
        "When ``proj_axis`` is 1, this corresponds to the global x axis. "
        "When ``proj_axis`` is 2, this corresponds to the global x axis. ",
        units=MICROMETER,
    )

    y: ObsGridArray = pydantic.Field(
        ...,
        title="Local y Observation Coordinates",
        description="Local y observation coordinates w.r.t. ``local_origin`` and ``proj_axis``. "
        "When ``proj_axis`` is 0, this corresponds to the global z axis. "
        "When ``proj_axis`` is 1, this corresponds to the global z axis. "
        "When ``proj_axis`` is 2, this corresponds to the global y axis. ",
        units=MICROMETER,
    )

    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # stores 1 complex number per pair of grid points, per frequency,
        # for Er, Etheta, Ephi, Hr, Htheta, and Hphi (6 components)
        return BYTES_COMPLEX * len(self.x) * len(self.y) * len(self.freqs) * 6


class FieldProjectionKSpaceMonitor(AbstractFieldProjectionMonitor):
    """:class:`Monitor` that samples electromagnetic near fields in the frequency domain
    and projects them on an observation plane defined in k-space. The ``center`` and ``size``
    fields define where the monitor will be placed in order to record near fields, typically
    very close to the structure of interest. The near fields are then
    projected to far-field locations defined in k-space by ``ux``, ``uy``, and ``proj_distance``,
    relative to the ``custom_origin``. Here, ``ux`` and ``uy`` are associated with a local
    coordinate system where the local 'z' axis is defined by ``proj_axis``: which is the axis
    normal to this monitor. If the distance between the near and far field locations is much
    larger than the size of the device, one can typically set ``far_field_approx`` to ``True``,
    which will make use of the far-field approximation to speed up calculations. If the
    projection distance is comparable to the size of the device, we recommend setting
    ``far_field_approx`` to ``False``, so that the approximations are not used, and the
    projection is accurate even just a few wavelengths away from the near field locations.
    For applications where the monitor is an open surface rather than a box that
    encloses the device, it is advisable to pick the size of the monitor such that the
    recorded near fields decay to negligible values near the edges of the monitor.

    Example
    -------
    >>> monitor = FieldProjectionKSpaceMonitor(
    ...     center=(1,2,3),
    ...     size=(2,2,2),
    ...     freqs=[250e12, 300e12],
    ...     name='n2f_monitor',
    ...     custom_origin=(1,2,3),
    ...     proj_axis=2,
    ...     ux=[0.1,0.2],
    ...     uy=[0.3,0.4,0.5]
    ...     )
    """

    proj_axis: Axis = pydantic.Field(
        ...,
        title="Projection Plane Axis",
        description="Axis along which the observation plane is oriented.",
    )

    proj_distance: float = pydantic.Field(
        1e6,
        title="Projection Distance",
        description="Radial distance of the projection points from ``local_origin``.",
        units=MICROMETER,
    )

    ux: ObsGridArray = pydantic.Field(
        ...,
        title="Normalized kx",
        description="Local x component of wave vectors on the observation plane, "
        "relative to ``local_origin`` and oriented with respect to ``proj_axis``, "
        "normalized by (2*pi/lambda) where lambda is the wavelength "
        "associated with the background medium. Must be in the range [-1, 1].",
    )

    uy: ObsGridArray = pydantic.Field(
        ...,
        title="Normalized ky",
        description="Local y component of wave vectors on the observation plane, "
        "relative to ``local_origin`` and oriented with respect to ``proj_axis``, "
        "normalized by (2*pi/lambda) where lambda is the wavelength "
        "associated with the background medium. Must be in the range [-1, 1].",
    )

    @pydantic.root_validator()
    def reciprocal_vector_range(cls, values):
        """Ensure that ux, uy are in [-1, 1]."""
        maxabs_ux = max(list(values.get("ux")), key=abs)
        maxabs_uy = max(list(values.get("uy")), key=abs)
        name = values.get("name")
        if maxabs_ux > 1:
            raise SetupError(f"Entries of 'ux' must lie in the range [-1, 1] for monitor {name}.")
        if maxabs_uy > 1:
            raise SetupError(f"Entries of 'uy' must lie in the range [-1, 1] for monitor {name}.")
        return values

    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # stores 1 complex number per pair of grid points, per frequency,
        # for Er, Etheta, Ephi, Hr, Htheta, and Hphi (6 components)
        return BYTES_COMPLEX * len(self.ux) * len(self.uy) * len(self.freqs) * 6


class DiffractionMonitor(PlanarMonitor, FreqMonitor):
    """:class:`Monitor` that uses a 2D Fourier transform to compute the
    diffraction amplitudes and efficiency for allowed diffraction orders.

    Example
    -------
    >>> monitor = DiffractionMonitor(
    ...     center=(1,2,3),
    ...     size=(inf,inf,0),
    ...     freqs=[250e12, 300e12],
    ...     name='diffraction_monitor',
    ...     normal_dir='+',
    ...     )
    """

    normal_dir: Direction = pydantic.Field(
        "+",
        title="Normal Vector Orientation",
        description="Direction of the surface monitor's normal vector w.r.t. "
        "the positive x, y or z unit vectors. Must be one of ``'+'`` or ``'-'``. "
        "Defaults to ``'+'`` if not provided.",
    )

    colocate: Literal[False] = pydantic.Field(
        False,
        title="Colocate Fields",
        description="Defines whether fields are colocated to grid cell boundaries (i.e. to the "
        "primal grid) on-the-fly during a solver run. Can be toggled for field recording monitors "
        "and is hard-coded for other monitors depending on their specific function.",
    )

    @pydantic.validator("size", always=True)
    def diffraction_monitor_size(cls, val):
        """Ensure that the monitor is infinite in the transverse direction."""
        if val.count(inf) != 2:
            raise SetupError(
                "A 'DiffractionMonitor' must have a size of 'td.inf' along both "
                f"transverse directions, given size={val}."
            )
        return val

    def storage_size(self, num_cells: int, tmesh: ArrayFloat1D) -> int:
        """Size of monitor storage given the number of points after discretization."""
        # assumes 1 diffraction order per frequency; actual size will be larger
        return BYTES_COMPLEX * len(self.freqs)


# types of monitors that are accepted by simulation
MonitorType = Union[
    FieldMonitor,
    FieldTimeMonitor,
    PermittivityMonitor,
    FluxMonitor,
    FluxTimeMonitor,
    ModeMonitor,
    ModeSolverMonitor,
    FieldProjectionAngleMonitor,
    FieldProjectionCartesianMonitor,
    FieldProjectionKSpaceMonitor,
    DiffractionMonitor,
]
