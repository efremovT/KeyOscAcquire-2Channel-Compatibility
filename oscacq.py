# -*- coding: utf-8 -*-
"""
The PyVISA communication with the oscilloscope.

See Keysight's Programmer's Guide for reference on the VISA commands.

Andreas Svela // 2019
"""

__docformat__ = "restructuredtext en"

import os
import sys
import pyvisa
import time
import datetime as dt
import numpy as np
import matplotlib.pyplot as plt
import logging; _log = logging.getLogger(__name__)

# local file with default options:
import keyoscacquire.config as config
import keyoscacquire.auxiliary as auxiliary
import keyoscacquire.traceio as traceio

# for compatibility (discouraged to use)
from keyoscacquire.traceio import save_trace, save_trace_npy, plot_trace

#: Supported Keysight DSO/MSO InfiniiVision series
_supported_series = ['1000', '2000', '3000', '4000', '6000']
#: Keysight colour map for the channels
_screen_colors = {1:'C1', 2:'C2', 3:'C0', 4:'C3'}
#: Datatype is ``'h'`` for 16 bit signed int (``WORD``), ``'b'`` for 8 bit signed bit (``BYTE``).
#: Same naming as for structs `docs.python.org/3/library/struct.html#format-characters`
_datatypes = {'BYT':'b', 'WOR':'h', 'BYTE':'b', 'WORD':'h'}


## ========================================================================= ##

class Oscilloscope:
    """PyVISA communication with the oscilloscope.

    Init opens a connection to an instrument and chooses default settings
    for the connection and acquisition.

    Leading underscores indicate that an attribute or method is read-only or
    suggested to be for interal use only.

    Parameters
    ----------
    address : str, default :data:`~keyoscacquire.config._visa_address`
        Visa address of instrument. To find the visa addresses of the instruments
        connected to the computer run ``list_visa_devices`` in the command line.
        Example address ``'USB0::1234::1234::MY1234567::INSTR'``
    timeout : int, default :data:`~keyoscacquire.config._timeout`
        Milliseconds before timeout on the channel to the instrument
    verbose : bool, default ``True``
        If ``True``: prints when the connection to the device is opened etc,
        and sets attr:`verbose_acquistion` to ``True``

    Raises
    ------
    :class:`pyvisa.errors.Error`
        if no successful connection is made.

    Attributes
    ----------
    verbose : bool
        If ``True``: prints when the connection to the device is opened, the
        acquistion mode, etc
    verbose_acquistion : bool
        If ``True``: prints that the capturing starts and the number of points
        captured
    fname : str, default :data:`keyoscacquire.config._filename`
        The filename to which the trace will be saved with :meth:`save_trace()`
    ext : str, default :data:`keyoscacquire.config._filetype`
        The extension for saving traces, must include the period, e.g. ``.csv``
    savepng : bool, default :data:`keyoscacquire.config._export_png`
        If ``True``: will save a png of the plot when :meth:`save_trace()`
    showplot : bool, default :data:`keyoscacquire.config._show_plot`
        If ``True``: will show a matplotlib plot window when :meth:`save_trace()`
    _inst : :class:`pyvisa.resources.Resource`
        The oscilloscope PyVISA resource
    _id : str
        The maker, model, serial and firmware version of the scope. Examples::

            'AGILENT TECHNOLOGIES,DSO-X 2024A,MY1234567,12.34.567891234'
            'KEYSIGHT TECHNOLOGIES,MSO9104A,MY12345678,06.30.00609'

    _model : str
        The instrument's model name
    _serial : str
        The instrument's serial number
    _address : str
        Visa address of instrument
    _time : :class:`~numpy.ndarray`
        The time axis of the most recent captured trace
    _values : :class:`~numpy.ndarray`
        The values for the most recent captured trace
    _capture_channels : list of ints
        The channels of captured for the most recent trace
    """
    _raw = None
    _metadata = None
    _time = None
    _values = None
    fname = config._filename
    ext = config._filetype
    savepng = config._export_png
    showplot = config._show_plot

    def __init__(self, address=config._visa_address, timeout=config._timeout, verbose=True):
        """See class docstring"""
        self._address = address
        self.verbose = verbose
        self.verbose_acquistion = verbose
        # Connect to the scope
        try:
            rm = pyvisa.ResourceManager()
            self._inst = rm.open_resource(address)
        except pyvisa.Error as err:
            print(f"\n\nCould not connect to '{address}', see traceback below:\n")
            raise
        self._timeout = timeout
        # For TCP/IP socket connections enable the read Termination Character, or reads will timeout
        if self._inst.resource_name.endswith('SOCKET'):
            self._inst.read_termination = '\n'
        # Clear the status data structures, the device-defined error queue, and the Request-for-OPC flag
        self.write('*CLS')
        # Make sure WORD and BYTE data is transeferred as signed ints and lease significant bit first
        self.write(':WAVeform:UNSigned OFF')
        self.write(':WAVeform:BYTeorder LSBFirst') # MSBF is default, must be overridden for WORD to work
        # Get information about the connected device
        self._id = self.query('*IDN?')
        try:
            maker, self._model, self._serial, _, self._model_series = auxiliary.interpret_visa_id(self._id)
            if self.verbose:
                print(f"Connected to {maker} {self._model} {self._serial}'")
        except Exception:
            if self.verbose:
                print(f"Connected to '{self._id}'")
            print("(!) Failed to intepret the VISA id")
        if not self._model_series in _supported_series:
                print("(!) WARNING: This model (%s) is not yet fully supported by keyoscacquire," % self._model)
                print("             but might work to some extent. keyoscacquire supports Keysight's")
                print("             InfiniiVision X-series oscilloscopes.")
        # Populate attributes and set standard settings
        if self.verbose:
            print("Using settings:")
        self.set_acquiring_options(wav_format=config._waveform_format, acq_type=config._acq_type,
                                   num_averages=config._num_avg, p_mode='RAW', num_points=0,
                                   verbose_acquistion=verbose)
        print("  ", end="")
        self.set_channels_for_capture(channels=config._ch_nums)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def write(self, command):
        """Write a VISA command to the oscilloscope.

        Parameters
        ----------
        command : str
            VISA command to be written"""
        self._inst.write(command)

    def query(self, command, action=""):
        """Query a VISA command to the oscilloscope. Will ask the oscilloscope
        for the latest error if the query times out.

        Parameters
        ----------
        command : str
            VISA query
        action : str, default ""
            Optional argument used to customise the error message if there is a
            timeout
        """
        try:
            return self._inst.query(command).strip()
        except pyvisa.Error as err:
            if action:
                msg = f"{action} (command '{command}')"
            else:
                msg = f"query '{command}'"
            print(f"\nVisaError: {err}\n  When trying {msg}.")
            print(f"  Have you checked that the timeout (currently {self._timeout:,d} ms) is sufficently long?")
            try:
                print(f"Latest error from the oscilloscope: '{self.get_error()}'\n")
            except Exception:
                print("Could not retrieve error from the oscilloscope")
            raise

    def close(self, set_running=True):
        """Closes the connection to the oscilloscope.

        Parameters
        ----------
        set_running : bool, default ``True``
            ``True`` sets the oscilloscope to running before closing the
            connection, ``False`` leaves it in its current state
        """
        # Set the oscilloscope running before closing the connection
        if set_running:
            self.run()
        self._inst.close()
        _log.debug(f"Closed connection to '{self._id}'")

    def get_error(self):
        """Get the latest error

        Returns
        -------
        str
            error number,description
        """
        # Do not use self.query here as that can lead to infinite nesting!
        return self._inst.query(":SYSTem:ERRor?").strip()

    def run(self):
        """Set the ocilloscope to running mode."""
        self.write(':RUN')

    def stop(self):
        """Stop the oscilloscope."""
        self.write(':STOP')

    def is_running(self):
        """Determine if the oscilloscope is running.

        Returns
        -------
        bool
            ``True`` if running, ``False`` otherwise
        """
        # The third bit of the operation register is 1 if the instrument is running
        reg = int(self.query(':OPERegister:CONDition?'))
        return (reg & 8) == 8

    @property
    def timeout(self):
        """The timeout on the VISA communication with the instrument. The
        timeout must be longer than the acquisition time.

        :getter:  Returns the number of milliseconds before timeout of a query command
        :setter:  Set the number of milliseconds before timeout of a query command
        :type:    int
        """
        return self._inst.timeout

    @timeout.setter
    def timeout(self, timeout: int):
        """See getter"""
        self._inst.timeout = val

    @property
    def active_channels(self):
        """Find the currently active channels on the instrument

        .. note:: Changing the active channels will not affect with channels are
          captured unless :meth:`set_channels_for_capture()` is subsequently run.
          The :meth:`get_traces()` family of methods will make sure of this.

        :getter:  Returns a list of the active channels, for example ``[1, 3]``
        :setter:  list of the active channels, for example ``[1, 3]``
        :type:    list of ints
        """
        # querying DISP for each channel to determine which channels are currently displayed
        return [i for i in range(1, 5) if bool(int(self.query(f":CHAN{i}:DISP?")))]

    @active_channels.setter
    def active_channels(self, channels: list):
        """See getter"""
        if not isinstance(channels, list):
            channels = [channels]
        for i in range(1, 5):
            self.write(f":CHAN{i}:DISP {int(i in channels)}")

    @property
    def acq_type(self):
        """Acquisition mode of the oscilloscope

        Choose between

        * ``'NORMal'`` ??? sets the oscilloscope in the normal mode.
        * ``'AVERage'`` or ``'AVER<m>'`` ??? sets the oscilloscope in the averaging mode.
          The number of averages can be set with :attr:`num_averages`, or
          <m> will be used as :attr:`num_averages` if supplied.
          <m> can be in the range 2 to 65,536
        * ``'HRESolution'`` ??? sets the oscilloscope in the high-resolution mode
          (also known as smoothing). This mode is used to reduce noise at slower
          sweep speeds where the digitizer samples faster than needed to fill memory for the displayed time range.
            For example, if the digitizer samples at 200 MSa/s, but the effective sample rate is 1 MSa/s
            (because of a slower sweep speed), only 1 out of every 200 samples needs to be stored.
            Instead of storing one sample (and throwing others away), the 200 samples are averaged
            together to provide the value for one display point. The slower the sweep speed, the greater
            the number of samples that are averaged together for each display point.

        :getter:  Returns the current mode (will not return ``<m>`` for ``AVER``)
        :setter:  Sets the mode, for example ``AVER8``, if :attr:`verbose` will
                  print the type and the number of averages number
        :type:    ``{'NORMal', 'AVERage', 'AVER<m>', 'HRES'}``

        Raises
        ------
        ValueError
            If ``<m>`` in cannot be converted to an int (or is out of range)
        """
        return self.query(":ACQuire:TYPE?")

    @acq_type.setter
    def acq_type(self, type: str):
        """See getter"""
        acq_type = type[:4].upper()
        self.write(f":ACQuire:TYPE {acq_type}")
        if self.verbose:
            print(f"  Acquisition type:  {acq_type}")
        # Handle AVER<m> expressions
        if acq_type == 'AVER':
            if len(type) > 4 and not type[4:].lower() == 'age':
                try:
                    self.num_averages = int(type[4:])
                except ValueError:
                    ValueError(f"\nValueError: Failed to convert '{type[4:]}' to an integer, "
                                "check that acquisition type is on the form AVER or AVER<m> "
                               f"where <m> is an integer (currently acq. type is '{type}').\n")
            else:
                num = self.num_averages
                if self.verbose:
                    print(f"  # of averages:  {num}")

    @property
    def num_averages(self):
        """The number of averages taken if the scope is in the ``'AVERage'``
        :attr:`acq_type`

        :getter:  Returns the current number of averages
        :setter:  Set the number, will print the number if :attr:`verbose`
        :type:    int, 2 to 65,536

        Raises
        ------
        ValueError
            If the number is is out of range
        """
        return self.query(":ACQuire:COUNt?")

    @num_averages.setter
    def num_averages(self, num: int):
        """See getter"""
        if not (2 <= num <= 65536):
                raise ValueError(f"\nThe number of averages {num} is out of range.")
        self.write(f":ACQuire:COUNt {num}")
        if self.verbose and self.acq_type == 'AVER':
            print(f"  # of averages:  {num}")

    @property
    def p_mode(self):
        """The points mode of the acquistion

        ``'NORMal'`` is limited to 62,500 points, whereas ``'RAW'`` gives up to
        1e6 points. Use ``'MAXimum'`` for sources that are not analogue or digital.

        :getter:  Returns the current mode
        :setter:  Set the mode, will check if compatible with the :attr:`acq_type`
        :type:    ``{'NORMal', 'RAW', 'MAXimum'}``
        """
        return self.query(":WAVeform:POINts:MODE?")

    @p_mode.setter
    def p_mode(self, p_mode: str):
        """See getter"""
        if (not p_mode[:4] == 'NORM') and self.acq_type == 'AVER':
            p_mode = 'NORM'
            _log.info(f":WAVeform:POINts:MODE overridden (from {p_mode}) to "
                        "NORMal due to :ACQuire:TYPE:AVERage.")
        self.write(f":WAVeform:POINts:MODE {p_mode}")

    @property
    def num_points(self):
        """The number of points to be acquired for each channel. Use 0 to
        get the maximum number given the :attr:`p_mode`, or override with a
        lower number than maximum for the given :attr:`p_mode`

        .. warning:: If the exact number of points is crucial, always check the
          number of points with the getter after performing the setter.

        .. note:: The scope must be stopped to get the number of points that
          will be transferred when it is in the *stopped* state. As this package
          always stops the scope when getting a trace, the getter will also
          do this to get the actual number of points that will be
          transferred (otherwise the returned number will be capped by the
          :attr:`p_mode` ``NORMal`` (which can be transferred without
          stopping the scope)).

        :getter:  Returns the number of points that will be acquired (stopping
                  and re-running the scope as explained in the note above)
        :setter:  Set the number, but beware that the scope might change the
                  number depending on memory depth, time axis settings, etc.
        :type:    int
        """
        # Must stop the scope to be able to read the actual number of points
        # that will be transferred in the RAW or MAX mode
        self.stop()
        points = int(self.query(":WAVeform:POINTs?"))
        self.run()
        return points

    @num_points.setter
    def num_points(self, num_points: int):
        """See getter"""
        if num_points == 0:
            self.write(f":WAVeform:POINts MAXimum")
            _log.debug("Number of points set to: MAX")
        # If number of points has been specified, tell the instrument to
        # use this number of points
        elif num_points > 0:
            if self._model_series in ['9000']:
                self.write(f":ACQuire:POINts {num_points}")
            else:
                # Must stop the scope to set the number of points to avoid
                # getting an error in the scopes' log (however, it seems to
                # be working regardless, only the get_error() will return -222)
                self.stop()
                self.write(f":WAVeform:POINts {num_points}")
                self.run()
            _log.debug(f"Number of points set to:  {num_points}")

    @property
    def wav_format(self):
        """Data transmission mode for waveform data points, i.e. how
        the data is formatted when sent from the oscilloscope.

        * ``'ASCii'`` formatted data converts the internal integer data values
           to real Y-axis values. Values are transferred as ascii digits in
           floating point notation, separated by commas.
        * ``'WORD'`` formatted data transfers signed 16-bit data as two bytes.
        * ``'BYTE'`` formatted data is transferred as signed 8-bit bytes.

        :getter:  Returns the number of points that will be acquired, however
                  it does not seem to be fully stable
        :setter:  Set the number, but beware that the scope might change the
                  number depending on memory depth, time axis settings, etc.
        :type:    ``{'WORD', 'BYTE', 'ASCii'}``
        """
        return self.query(":WAVeform:FORMat?")

    @wav_format.setter
    def wav_format(self, wav_format: str):
        """See getter"""
        self.write(f":WAVeform:FORMat {wav_format}")

    def set_acquiring_options(self, wav_format=None, acq_type=None,
                              num_averages=None, p_mode=None, num_points=None,
                              verbose_acquistion=None):
        """Change acquisition options

        Parameters
        ----------
        wav_format : {``'WORD'``, ``'BYTE'``, ``'ASCii'``}, default :data:`keyoscacquire.config._waveform_format`
            Select the format of the communication of waveform from the
            oscilloscope, see :attr:`wav_format`
        acq_type : {``'HRESolution'``, ``'NORMal'``, ``'AVERage'``, ``'AVER<m>'``}, default :data:`keyoscacquire.config._acq_type`
            Acquisition mode of the oscilloscope. <m> will be used as
            num_averages if supplied, see :attr:`acq_type`
        num_averages : int, 2 to 65536, default :data:`keyoscacquire.config._num_avg`
            Applies only to the ``'AVERage'`` mode: The number of averages applied
        p_mode : {``'NORMal'``, ``'RAW'``, ``'MAXimum'``}, default ``'RAW'``
            ``'NORMal'`` is limited to 62,500 points, whereas ``'RAW'`` gives up to 1e6 points.
            Use ``'MAXimum'`` for sources that are not analogue or digital
        num_points : int, default 0
            Use 0 to get the maximum amount of points, otherwise
            override with a lower number than maximum for the :attr:`p_mode`
        verbose_acquistion : bool or ``None``, default ``None``
            Temporarily control attribute which decides whether to print
            information while acquiring: bool sets it to the bool value,
            ``None`` leaves as the it is in the Oscilloscope object

        Raises
        ------
        ValueError
            If num_averages are outside of the range or <m> in acq_type cannot
            be converted to int
        """
        if verbose_acquistion is not None:
            self.verbose_acquistion = verbose_acquistion
        if acq_type is not None:
            self.acq_type = acq_type
        if num_averages is not None:
            self.num_averages = num_averages
        # Set options for waveform export
        self.set_waveform_export_options(wav_format, num_points, p_mode)

    def set_waveform_export_options(self, wav_format=None, num_points=None, p_mode=None):
        """
        Set options for the waveform export from the oscilloscope to the computer

        Parameters
        ----------
        wav_format : {``'WORD'``, ``'BYTE'``, ``'ASCii'``}, default :data:`~keyoscacquire.config._waveform_format`
            Select the format of the communication of waveform from the
            oscilloscope, see :attr:`wav_format`
        p_mode : {``'NORMal'``, ``'RAW'``}, default ``'RAW'``
            ``'NORMal'`` is limited to 62,500 points, whereas ``'RAW'`` gives up to 1e6 points.
        num_points : int, default 0
            Use 0 to get the maximum amount of points, otherwise
            override with a lower number than maximum for the :attr:`p_mode`
        """
        # Choose format for the transmitted waveform
        if wav_format is not None:
            self.wav_format = wav_format
        if p_mode is not None:
            self.p_mode = p_mode
        if num_points is not None:
            self.num_points = num_points

    ## Capture and read functions ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##

    def set_channels_for_capture(self, channels=None):
        """Decide the channels to be acquired, or determine by checking active
        channels on the oscilloscope.

        Parameters
        ----------
        channels : list of ints or ``'active'``, default :data:`~keyoscacquire.config._ch_nums`
            list of the channel numbers to be acquired, example ``[1, 3]``.
            Use ``'active'`` or ``[]`` to capture all the currently active
            channels on the oscilloscope.

        Returns
        -------
        list of ints
            the channels that will be captured, example ``[1, 3]``
        """
        # If no channels specified, find the channels currently active and acquire from those
        if np.any(channels in [[], ['active'], 'active']) or (self._capture_active and channels is None):
            self._capture_channels = self.active_channels
            # Store that active channels are being used
            self._capture_active = True
        else:
            self._capture_channels =  channels
            self._capture_active = False
        # Build list of sources
        self._sources = [f"CHAN{ch}" for ch in self._capture_channels]
        if self.verbose_acquistion:
            print(f"Acquire from channels:  {self._capture_channels}")
        return self._capture_channels

    def capture_and_read(self, set_running=True):
        """Acquire raw data from selected channels according to acquring options
        currently set with :func:`set_acquiring_options`.
        The parameters are provided by :func:`set_channels_for_capture`.

        The populated attributes raw and metadata should be processed
        by :func:`process_data`.

        raw : :class:`~numpy.ndarray`
            An ndarray of ints that can be converted to voltage values using the preamble.
        metadata
            depends on the :attr:`wav_format`

        Parameters
        ----------
        set_running : bool, default ``True``
            ``True`` leaves oscilloscope running after data capture

        Raises
        ------
        ValueError
            If :attr:`wav_format` is not one of ``{'BYTE', 'WORD', 'ASCii'}``

        See also
        --------
        :func:`process_data`
        """
        ## Capture data
        if self.verbose_acquistion:
            print("Start acquisition..")
        start_time = time.time() # time the acquiring process
        # If the instrument is not running, we presumably want the data
        # on the screen and hence don't want to use DIGitize as digitize
        # will obtain a new trace.
        if self.is_running():
            # DIGitize is a specialized RUN command.
            # Waveforms are acquired according to the settings of the :ACQuire commands.
            # When acquisition is complete, the instrument is stopped.
            self.write(':DIGitize ' + ", ".join(self._sources))
        ## Read from the scope
        wav_format = self.wav_format[:3]
        if wav_format in ['WOR', 'BYT']:
            self._read_binary(datatype=_datatypes[wav_format])
        elif wav_format[:3] == 'ASC':
            self._read_ascii()
        else:
            raise ValueError(f"Could not capture and read data, waveform format "
                             f"'{wav_format}' is unknown.\n")
        ## Print to log
        to_log = f"Elapsed time capture and read: {(time.time()-start_time)*1e3:.1f} ms"
        if self.verbose_acquistion:
            _log.info(to_log)
        else:
            _log.debug(to_log)
        if set_running:
            self.run()

    def _read_binary(self, datatype='standard'):
        """Read data and metadata from sources of the oscilloscope
        when waveform format is ``'WORD'`` or ``'BYTE'``.

        The parameters are provided by :func:`set_channels_for_capture`.
        The output should be processed by :func:`process_data_binary`.

        Populates the following attributes
        raw : :class:`~numpy.ndarray`
            Raw data to be processed by :func:`process_data_binary`.
            An ndarray of ints that can be converted to voltage values using the preamble.
        metadata : list of str
            List of preamble metadata (comma separated ascii values) for each channel

        Parameters
        ----------
        datatype : char or ``'standard'``, optional but must match waveform format used
            To determine how to read the values from the oscilloscope depending
            on :attr:`wav_format`. Datatype is ``'h'`` for 16 bit signed int
            (``'WORD'``), for 8 bit signed bit (``'BYTE'``) (same naming as for
            structs, `https://docs.python.org/3/library/struct.html#format-characters`).
            ``'standard'`` will evaluate :data:`oscacq._datatypes[self.wav_format]`
             to automatically choose according to the waveform format
        set_running : bool, default ``True``
            ``True`` leaves oscilloscope running after data capture
        """
        self._raw, self._metadata = [], []
        # Loop through all the sources
        for source in self._sources:
            # Select the channel for which the succeeding WAVeform commands applies to
            self.write(f":WAVeform:SOURce {source}")
            try:
                # obtain comma separated metadata values for processing of raw data for this source
                self._metadata.append(self.query(':WAVeform:PREamble?'))
                # obtain the data
                # read out data for this source
                self._raw.append(self._inst.query_binary_values(':WAVeform:DATA?',
                                                               datatype=datatype,
                                                               container=np.array))
            except pyvisa.Error as err:
                print(f"\n\nVisaError: {err}\n  When trying to obtain the waveform.")
                print(f"  Have you checked that the timeout (currently {self._timeout:,d} ms) is sufficently long?")
                try:
                    print(f"Latest error from the oscilloscope: '{self.get_error()}'\n")
                except Exception:
                    print("Could not retrieve error from the oscilloscope")
                raise

    def _read_ascii(self):
        """Read data and metadata from sources of the oscilloscope
        when waveform format is ASCii.

        The parameters are provided by :func:`set_channels_for_capture`.
        The output should be processed by :func:`process_data_ascii`.

        Populates the following attributes
        raw : str
            Raw data to be processed by :func:`process_data_ascii`.
            The raw data is a list of one IEEE block per channel with a head
            and then comma separated ascii values.
        metadata : tuple of str
            Tuple of the preamble for one of the channels to calculate time
            axis (same for all channels) and the model series

        Parameters
        ----------
        set_running : bool, default ``True``
            ``True`` leaves oscilloscope running after data capture
        """
        self._raw = []
        # Loop through all the sources
        for source in self._sources:
            # Select the channel for which the succeeding WAVeform commands applies to
            self.write(f":WAVeform:SOURce {source}")
            # Read out data for this source
            self._raw.append(self.query(':WAVeform:DATA?', action="obtain the waveform"))
        # Get the preamble (used for calculating time axis, which is the same
        # for all channels)
        preamble = self.query(':WAVeform:PREamble?')
        self._metadata = (preamble, self._model_series)


    ## Building functions to get a trace and various option setting and processing ##

    def get_trace(self, channels=None, verbose_acquistion=None):
        """Obtain one trace with current settings. Will return the values
        of the traces, but alos populate a few attributes, including
        ``_time``, ``_values`` and ``_capture_channels``.

        Use :meth:`save_trace()` to save the trace to disk.

        Parameters
        ----------
        channels : list of ints or ``'active'``, default :data:`~keyoscacquire.config._ch_nums`
            Optionally change the list of the channel numbers to be acquired,
            example ``[1, 3]``. Use ``'active'`` or ``[]`` to capture all the
            currently active channels on the oscilloscope.
        verbose_acquistion : bool or ``None``, default ``None``
            Optionally change :attr:`verbose_acquistion`

        Returns
        -------
        _time : :class:`~numpy.ndarray`
            Time axis for the measurement
        _values : :class:`~numpy.ndarray`
            Voltage values, same sequence as sources input, each row
            represents one channel
        _capture_channels : list of ints
            list of the channels obtaied from, example ``[1, 3]``
        """
        self.set_channels_for_capture(channels=channels)
        # Possibility to override verbose_acquistion
        if verbose_acquistion is not None:
            self.verbose_acquistion = verbose_acquistion
        # Capture, read and process data
        self.capture_and_read()
        self._time, self._values = process_data(self._raw, self._metadata, self.wav_format,
                                                verbose_acquistion=self.verbose_acquistion)
        return self._time, self._values, self._capture_channels

    def set_options_get_trace(self, channels=None, wav_format=None, acq_type=None,
                              num_averages=None, p_mode=None, num_points=None):
        """Set the options provided by the parameters and obtain one trace.

        Parameters
        ----------
        channels : list of ints or ``'active'``, default :data:`~keyoscacquire.config._ch_nums`
            list of the channel numbers to be acquired, example ``[1, 3]``.
            Use ``'active'`` or ``[]`` to capture all the currently active
            channels on the oscilloscope.
        wav_format : {``'WORD'``, ``'BYTE'``, ``'ASCii'``}, default :data:`~keyoscacquire.config._waveform_format`
            Select the format of the communication of waveform from the
            oscilloscope, see :attr:`wav_format`
        acq_type : {``'HRESolution'``, ``'NORMal'``, ``'AVERage'``, ``'AVER<m>'``}, default :data:`~keyoscacquire.config._acq_type`
            Acquisition mode of the oscilloscope. <m> will be used as
            num_averages if supplied, see :attr:`acq_type`
        num_averages : int, 2 to 65536, default :data:`~keyoscacquire.config._num_avg`
            Applies only to the ``'AVERage'`` mode: The number of averages applied
        p_mode : {``'NORMal'``, ``'RAW'``, ``'MAXimum'``}, default ``'RAW'``
            ``'NORMal'`` is limited to 62,500 points, whereas ``'RAW'`` gives
            up to 1e6 points. Use ``'MAXimum'`` for sources that are not analogue or digital
        num_points : int, default 0
            Use 0 to let :attr:`p_mode` control the number of points, otherwise
            override with a lower number than maximum for the :attr:`p_mode`

        Returns
        -------
        _time : :class:`~numpy.ndarray`
            Time axis for the measurement
        _values : :class:`~numpy.ndarray`
            Voltage values, same sequence as sources input, each row
            represents one channel
        _capture_channels : list of ints
            list of the channels obtaied from, example ``[1, 3]``
        """
        ## Connect to instrument and specify acquiring settings
        self.set_acquiring_options(wav_format=wav_format, acq_type=acq_type,
                                   num_averages=num_averages, p_mode=p_mode,
                                   num_points=num_points)
        ## Capture, read and process data
        self.get_trace()
        return self._time, self._values, self._capture_channels

    def set_options_get_trace_save(self, fname=None, ext=None,
                                   channels=None, wav_format=None, acq_type=None,
                                   num_averages=None, p_mode=None,
                                   num_points=None, additional_header_info=None):
        """Get trace and save the trace to a file and plot to png.

        Filename is recursively checked to ensure no overwrite.
        The file header when capturing ch 1 and 3 in AVER8 is::

            # AGILENT TECHNOLOGIES,DSO-X 2024A,MY1234567,12.34.1234567890
            # AVER,8
            # 2019-09-06 20:01:15.187598
            # time,1,3

        Parameters
        ----------
        fname : str, default :data:`~keyoscacquire.config._filename`
            Filename of trace
        ext : str, default :data:`~keyoscacquire.config._filetype`
            Choose the filetype of the saved trace
        channels : list of ints or ``'active'``, default :data:`~keyoscacquire.config._ch_nums`
            list of the channel numbers to be acquired, example ``[1, 3]``.
            Use ``'active'`` or ``[]`` to capture all the currently active
            channels on the oscilloscope.
        wav_format : {``'WORD'``, ``'BYTE'``, ``'ASCii'``}, default :data:`~keyoscacquire.config._waveform_format`
            Select the format of the communication of waveform from the
            oscilloscope, see :attr:`wav_format`
        acq_type : {``'HRESolution'``, ``'NORMal'``, ``'AVERage'``, ``'AVER<m>'``}, default :data:`~keyoscacquire.config._acq_type`
            Acquisition mode of the oscilloscope. <m> will be used as
            num_averages if supplied, see :attr:`acq_type`
        num_averages : int, 2 to 65536, default :data:`~keyoscacquire.config._num_avg`
            Applies only to the ``'AVERage'`` mode: The number of averages applied
        p_mode : {``'NORMal'``, ``'RAW'``, ``'MAXimum'``}, default ``'RAW'``
            ``'NORMal'`` is limited to 62,500 points, whereas ``'RAW'`` gives up
            to 1e6 points. Use ``'MAXimum'`` for sources that are not analogue
            or digital
        num_points : int, default 0
            Use 0 to let :attr:`p_mode` control the number of points, otherwise
            override with a lower number than maximum for the :attr:`p_mode`
        additional_header_info : str, default ```None``
            Will put this string as a separate line before the column headers
        """
        self.set_options_get_trace(channels=channels, wav_format=wav_format,
                                   acq_type=acq_type, num_averages=num_averages,
                                   p_mode=p_mode, num_points=num_points)
        self.save_trace(fname, ext, additional_header_info=additional_header_info)

    def generate_file_header(self, channels=None, additional_line=None, timestamp=True):
        """Generate string to be used as file header for saved files

        The file header has this structure::

            <id>
            <mode>,<averages>
            <timestamp>
            additional_line
            time,<chs>

        Where ``<id>`` is the :attr:`~keyoscacquire.oscacq.Oscilloscope.id` of
        the oscilloscope, ``<mode>`` is the :attr:`~keyoscacquire.oscacq.Oscilloscope.acq_type`,
        ``<averages>`` :attr:`~keyoscacquire.oscacq.Oscilloscope.num_averages`
        (``"N/A"`` if not applicable) and ``<chs>`` are the comma separated
        channels used.

        .. note:: If ``additional_line`` is not supplied the fileheader will
          be four lines. If ``timestamp=False`` the timestamp line will not
          be present.

        Parameters
        ----------
        channels : list of strs or ints
            Any list of identifies for the channels used for the measurement to be saved.
        additional_line : str or ``None``, default ``None``
            No additional line if set to ``None``, otherwise the value of the argument will be used
            as an additonal line to the file header
        timestamp : bool
            ``True`` gives a line with timestamp, ``False`` removes the line

        Returns
        -------
        str
            string to be used as file header

        Example
        -------
        If the oscilloscope is acquiring in ``'AVER'`` mode with eight averages::

            Oscilloscope.generate_file_header([1, 'piezo'], additional_line="my comment")

        gives::

            # AGILENT TECHNOLOGIES,DSO-X 2024A,MY1234567,12.34.1234567890
            # AVER,8
            # 2019-09-06 20:01:15.187598
            # my comment
            # time,1,piezo

        """
        # Set num averages only if AVERage mode
        num_averages = self.num_averages if self.acq_type[:3] == 'AVE' else "N/A"
        mode_line = f"{self.acq_type},{num_averages}\n"
        # Set timestamp if called for
        timestamp_line = str(dt.datetime.now())+"\n" if timestamp else ""
        # Set addtional line if called for
        add_line = additional_line+"\n" if additional_line is not None else ""
        # Use _capture_channels unless channel argument is not None
        if channels is None:
            channels = self._capture_channels
        channels = [str(ch) for ch in channels]
        ch_str = ",".join(channels)
        channels_line = f"time,{ch_str}"
        return self._id+"\n"+mode_line+timestamp_line+add_line+channels_line

    def save_trace(self, fname=None, ext=None, additional_header_info=None,
                   savepng=None, showplot=None, nowarn=False):
        """Save the most recent trace to ``fname+ext``. Will check if the filename
        exists, and let the user append to the fname if that is the case.

        Parameters
        ----------
        fname : str, default :data:`~keyoscacquire.config._filename`
            Filename of trace
        ext : ``{'.csv', '.npy'}``, default :data:`~keyoscacquire.config._ext`
            Choose the filetype of the saved trace
        additional_header_info : str, default ```None``
            Will put this string as a separate line before the column headers
        savepng : bool, default :data:`~keyoscacquire.config._export_png`
            Choose whether to also save a png with the same filename
        showplot : bool, default :data:`~keyoscacquire.config._show_plot`
            Choose whether to show a plot of the trace
        """
        if not self._time is None:
            if fname is not None:
                self.fname = fname
            if ext is not None:
                self.ext = ext
            if savepng is not None:
                self.savepng = savepng
            if showplot is not None:
                self.showplot = showplot
            # Remove extenstion if provided in the fname
            if self.fname[-4:] in ['.npy', '.csv']:
                self.ext = self.fname[-4:]
                self.fname = self.fname[:-4]
            self.fname = auxiliary.check_file(self.fname, self.ext)
            traceio.plot_trace(self._time, self._values, self._capture_channels, fname=self.fname,
                               showplot=self.showplot, savepng=self.savepng)
            head = self.generate_file_header(additional_line=additional_header_info)
            traceio.save_trace(self.fname, self._time, self._values, fileheader=head, ext=self.ext,
                               print_filename=self.verbose_acquistion, nowarn=nowarn)
        else:
            print("(!) No trace has been acquired yet, use get_trace()")
            _log.info("(!) No trace has been acquired yet, use get_trace()")

    def plot_trace(self):
        """Plot and show the most recent trace"""
        if not self._time is None:
            traceio.plot_trace(self._time, self._values, self._capture_channels,
                               savepng=False, showplot=True)
        else:
            print("(!) No trace has been acquired yet, use get_trace()")
            _log.info("(!) No trace has been acquired yet, use get_trace()")

##============================================================================##
##                           DATA PROCESSING                                  ##
##============================================================================##

def process_data(raw, metadata, wav_format, verbose_acquistion=True):
    """Wrapper function for choosing the correct process_data function
    according to :attr:`wav_format` for the data obtained from
    :func:`Oscilloscope.capture_and_read`

    Parameters
    ----------
    raw : ~numpy.ndarray or str
        From :func:`~Oscilloscope.capture_and_read`: Raw data, type depending
        on :attr:`wav_format`
    metadata : list or tuple
        From :func:`~Oscilloscope.capture_and_read`: List of preambles or
        tuple of preamble and model series depending on :attr:`wav_format`.
        See :ref:`preamble`.
    wav_format : {``'WORD'``, ``'BYTE'``, ``'ASCii'``}
        Specify what waveform type was used for acquiring to choose the correct
        processing function.
    verbose_acquistion : bool
        True prints the number of points captured per channel

    Returns
    -------
    time : :class:`~numpy.ndarray`
        Time axis for the measurement
    y : :class:`~numpy.ndarray`
        Voltage values, each row represents one channel

    Raises
    ------
    ValueError
        If ``wav_format`` is not {'BYTE', 'WORD', 'ASCii'}

    See also
    --------
    :func:`Oscilloscope.capture_and_read`
    """
    if wav_format[:3] in ['WOR', 'BYT']:
        return _process_data_binary(raw, metadata, verbose_acquistion)
    elif wav_format[:3] == 'ASC':
        return _process_data_ascii(raw, metadata, verbose_acquistion)
    else:
        raise ValueError("Could not process data, waveform format \'{}\' is unknown.".format(wav_format))

def _process_data_binary(raw, preambles, verbose_acquistion=True):
    """Process raw 8/16-bit data to time values and y voltage values as received
    from :func:`Oscilloscope.capture_and_read_binary`.

    Parameters
    ----------
    raw : ~numpy.ndarray
        From :func:`~Oscilloscope.capture_and_read_binary`: An ndarray of ints
        that is converted to voltage values using the preamble.
    preambles : list of str
        From :func:`~Oscilloscope.capture_and_read_binary`: List of preamble
        metadata for each channel (list of comma separated ascii values,
        see :ref:`preamble`)
    verbose_acquistion : bool
        True prints the number of points captured per channel

    Returns
    -------
    time : :class:`~numpy.ndarray`
        Time axis for the measurement
    y : :class:`~numpy.ndarray`
        Voltage values, each row represents one channel
    """
    # Pick one preamble and use for calculating the time values (same for all channels)
    preamble = preambles[0].split(',')  # values separated by commas
    num_samples = int(float(preamble[2]))
    xIncr, xOrig, xRef = float(preamble[4]), float(preamble[5]), float(preamble[6])
    time = np.array([(np.arange(num_samples)-xRef)*xIncr + xOrig]) # compute x-values
    time = time.T # make x values vertical
    if verbose_acquistion:
        print(f"Points captured per channel: {num_samples:,d}")
        _log.info(f"Points captured per channel: {num_samples:,d}")
    y = np.empty((len(raw), num_samples))
    for i, data in enumerate(raw): # process each channel individually
        preamble = preambles[i].split(',')
        yIncr, yOrig, yRef = float(preamble[7]), float(preamble[8]), float(preamble[9])
        y[i,:] = (data-yRef)*yIncr + yOrig
    y = y.T # convert y to np array and transpose for vertical channel columns in csv file
    return time, y

def _process_data_ascii(raw, metadata, verbose_acquistion=True):
    """Process raw comma separated ascii data to time values and y voltage
    values as received from :func:`Oscilloscope.capture_and_read_ascii`

    Parameters
    ----------
    raw : str
        From :func:`~Oscilloscope.capture_and_read_ascii`: A string containing
        a block header and comma separated ascii values
    metadata : tuple
        From :func:`~Oscilloscope.capture_and_read_ascii`: Tuple of the
        preamble for one of the channels to calculate time axis (same for
        all channels) and the model series. See :ref:`preamble`.
    verbose_acquistion : bool
        True prints the number of points captured per channel

    Returns
    -------
    time : :class:`~numpy.ndarray`
        Time axis for the measurement
    y : :class:`~numpy.ndarray`
        Voltage values, each row represents one channel
    """
    preamble, model_series = metadata
    preamble = preamble.split(',')  # Values separated by commas
    num_samples = int(float(preamble[2]))
    xIncr, xOrig, xRef = float(preamble[4]), float(preamble[5]), float(preamble[6])
    # Compute time axis and wrap in extra [] to make it 2D
    time = np.array([(np.arange(num_samples)-xRef)*xIncr + xOrig])
    time = time.T # Make list vertical
    if verbose_acquistion:
        print(f"Points captured per channel: {num_samples:,d}")
        _log.info(f"Points captured per channel: {num_samples:,d}")
    y = []
    for data in raw:
        if model_series in ['2000']:
            data = data.split(data[:10])[1] # remove first 10 characters (IEEE block header)
        elif model_series in ['9000']:
            data = data.strip().strip(",") # remove newline character at the end of the string
        data = data.split(',') # samples separated by commas
        data = np.array([float(sample) for sample in data])
        y.append(data) # add ascii data for this channel to y array
    y = np.transpose(np.array(y))
    return time, y


## Module main function ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ##

def main():
    fname = sys.argv[1] if len(sys.argv) >= 2 else config._filename
    ext = config._filetype
    with Oscilloscope() as scope:
        scope.set_options_get_trace_save(fname, ext)


if __name__ == '__main__':
    main()
