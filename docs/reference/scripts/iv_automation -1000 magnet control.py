import numpy as np, nidaqmx, time, pyvisa
import warnings
from typing import Union
# connect to the instrument via pyvisa


class CustomError(Exception):
    def __init__(self, message="A custom error occurred"):
        self.message = message
        super().__init__(self.message)


class PyvisaInstrument:
    def __init__(self, address: str, name: str, termination: str, rm: pyvisa.ResourceManager):
        # instrument address
        self.address = address
        self.name = name
        # r/w termination, '\r' or '\n'
        self.termination = termination
        # pyvisa resource manager
        self.rm = rm
        self.my_instr = None
        self.timeout = None
        self.x_indexes = {}
        self.y_indexes = {}
        self.x_values = np.array([])
        self.y_values = np.array([])

    def connect(self):
        self.my_instr = self.rm.open_resource(self.address, timeout=self.timeout)
        self.my_instr.read_termination = self.termination
        self.my_instr.write_termination = self.termination

    def close(self):
        self.my_instr.close()
        self.my_instr = None

    def query(self, command: str, print_command=False, print_response=False):
        if print_command:
            print(command)
        response = self.my_instr.query(command)
        if print_response:
            print(response)
        return response
    
    def write(self, command: str, print_command=False):
        if print_command:
            print(command)
        return self.my_instr.write(command)
    
    def read(self, print_response=False):
        response = self.my_instr.read()
        if print_response:
            print(response)
        return response

    def receive_x(self, variable: str, value: float):
        self.x_values[self.x_indexes[variable]] = value

    def send_y(self, variable: str):
        return self.y_values[self.y_indexes[variable]]


# monochromator control
class MonoControl(PyvisaInstrument):
    def __init__(self, address: str, name: str, rm: pyvisa.ResourceManager, initial_wl: float):
        super().__init__(address, name, '\r', rm)
        self.type = 'mono'
        self.connect()
        self.get_identity()
        self.setup_scan()
        self.wl_goto(initial_wl)
        wavelength = self.get_wl()
        self.x_indexes['wavelength'] = 0
        self.y_indexes['measured_wavelength'] = 0
        self.x_values = np.array([initial_wl])
        self.y_values = np.array([wavelength])

    def get_identity(self):
        self.query('MODEL', print_response=True)

    def get_wl(self):
        reading = self.query('?NM')
        wavelength = float(reading.split(' ')[1])
        return wavelength

    def setup_scan(self, speed=300):
        statement = '%.2f NM/MIN' % speed
        self.query(statement, print_command=True, print_response=True)

    def wl_goto(self, wavelength: float):
        statement = '%.2f GOTO' % wavelength
        # check the format of the data here
        self.query(statement)


    # write the x value to the physical instrument
    def write_x(self):
        self.wl_goto(self.x_values[0])

    # read the y value from the physical instrument
    def read_y(self):
        self.y_values[0] = self.get_wl()
        return


# Keithley 2400 control


class KeithControl(PyvisaInstrument):
    def __init__(self, address: str, name: str, variable_name: str, rm: pyvisa.ResourceManager):
        super().__init__(address, name, '\n', rm)
        self.type = 'keithley'
        self.connect()
        self.get_identity()
        self.mode = 'volt_step'
        self.set_volt_step()
        self.x_indexes[variable_name] = 0
        self.y_indexes['measured_'+variable_name] = 0
        self.y_indexes[variable_name + '_leakage'] = 1
        volt, curr = self.read_curr()
        self.x_values = np.array([volt])
        self.y_values = np.array([volt, curr])

    def get_identity(self):
        print(self.query('*IDN?'))

    # for non-synchronized sweep only
    def set_volt_sweep(self, curr_compliance=1E-6, delay=0.01, volt_compliance=20):

        self.write(':SOUR:FUNC VOLT', print_command=True)
        self.write(':SENS:FUNC \'CURR\'', print_command=True)
        self.write(':SENS:CURR:PROT %.2e' % curr_compliance, print_command=True)
        self.write('SOUR:DEL %.3f' % delay, print_command=True)
        # turn on confield functions
        self.write(':SENS:FUNC:CONC ON', print_command=True)
        # set field reading
        self.write(':FORM:ELEM VOLT ,CURR', print_command=True)
        # turn on output
        self.write(':OUTP ON', print_command=True)
        # select sweep mode
        self.write(':SOUR:VOLT:MODE SWE', print_command=True)
        # select source ranging
        self.write(':SOUR:SWE:RANG %.0f' % volt_compliance, print_command=True)
        # select linear staircase sweep
        self.write(':SOUR:SWE:SPAC LIN', print_command=True)
        self.mode = 'volt_sweep'
        self.write(':OUTP ON', print_command=True)

    def set_volt_step(self, curr_compliance=1E-6, delay=0.1, volt_compliance=20):

        self.write(':SOUR:FUNC VOLT', print_command=True)
        self.write(':SENS:FUNC \'CURR\'', print_command=True)
        self.write(':SENS:CURR:PROT %.2e' % curr_compliance, print_command=True)
        self.write('SOUR:DEL %.3f' % delay, print_command=True)
        self.write(':SENS:FUNC:CONC ON', print_command=True)
        self.write(':FORM:ELEM VOLT ,CURR', print_command=True)
        self.write(':SOUR:VOLT:MODE FIXED', print_command=True)
        self.write(':SOUR:VOLT:RANG %.0f' % volt_compliance, print_command=True)
        self.write('TRIG:COUN 1', print_command=True)
        self.mode = 'volt_step'
        self.write(':OUTP ON', print_command=True)

    def volt_step(self, volt: float):
        if self.mode != 'volt_step':
            self.set_volt_step()
        self.write(':SOUR:VOLT:LEV %.3f' % volt)
        # self.query('READ?')

    def read_curr(self):
        if self.mode != 'volt_step':
            self.set_volt_step()
        volt, curr = self.read_float()
        return volt, curr

    def volt_sweep(self, start: float, stop: float, step: float):
        if self.mode != 'volt_sweep':
            self.set_volt_sweep()
        # select start
        self.write(':SOUR:VOLT:START %.3f' % start, print_command=True)
        # select stop
        self.write(':SOUR:VOLT:STOP %.3f' % stop, print_command=True)
        # select step
        if (stop - start) * step < 0:
            step = - step
        self.write(':SOUR:VOLT:STEP %.3f' % step, print_command=True)
        # set trigger count
        count = int((stop - start)/step + 1)
        self.write('TRIG:COUN %.0f' % count, print_command=True)
        # trigger sweep
        self.write('READ?', print_command=True)
        return count

    def read_float(self):
        raw_data = self.query('READ?')
        strings = raw_data.split(',')
        volt = float(strings[0])
        curr = float(strings[1])
        return volt, curr

    def read_numpy(self, dimension: int):
        raw_data = self.read()
        result = np.array(raw_data.split(',')).astype(np.float).reshape(dimension, -1)
        return result

    def write_x(self):
        self.volt_step(self.x_values[0])

    def read_y(self):
        volt, curr = self.read_float()
        self.y_values = np.array([volt, curr])


class DaqControl:

    def __init__(self, device_name: str):
        self.type = 'daq'
        self.device_name = device_name
        self.ai_task = nidaqmx.Task()
        self.ao_task = nidaqmx.Task()
        self.ai_index = 0
        self.ao_index = 0
        self.x_indexes = {}
        self.y_indexes = {}
        self.x_values = None
        self.y_values = None

    def add_ai_channel(self, address: str, variable: str):
        self.y_indexes[variable] = self.ai_index
        self.ai_task.ai_channels.add_ai_voltage_chan(self.device_name+'/'+address, max_val=10)
        self.read_y()
        self.ai_index += 1

    def add_ao_channel(self, address: str, variable: str):
        self.ao_task.ao_channels.add_ao_voltage_chan(self.device_name + '/' + address)
        self.x_indexes[variable] = self.ao_index
        self.ai_task.ai_channels.add_ai_voltage_chan(self.device_name+'/_'+address+'_vs_aognd', max_val=10)
        self.y_indexes['measured_'+variable] = self.ai_index
        self.read_y()
        if self.x_values is None:
            self.x_values = np.array(self.y_values[self.ai_index]).reshape(-1)
        else:
            self.x_values = np.append(self.x_values, self.y_values[self.ai_index])
        self.ai_index += 1
        self.ao_index += 1

    def receive_x(self, variable: str, value: float):
        self.x_values[self.x_indexes[variable]] = value

    def write_x(self):
        self.ao_task.write(self.x_values)

    def read_y(self):
        self.y_values = np.array(self.ai_task.read()).reshape(-1)

    def send_y(self, variable: float):
        return self.y_values[self.y_indexes[variable]]

    def check_status(self):
        for key, index in self.x_indexes.items():
            print(key, self.x_values[index])
        for key, index in self.y_indexes.items():
            print(key, self.y_values[index])


class YChannelCollection:

    def __init__(self):
        self.field_index = 0
        self.y_indexes = {}
        self.variable_name_list = []
        self.instrument_list = []
        self.value_list = []

    def add_y(self, variable: str, instrument: Union[PyvisaInstrument, DaqControl], value: float):
        self.y_indexes[variable] = self.field_index
        self.variable_name_list.append(variable)
        self.instrument_list.append(instrument)
        self.value_list.append(value)
        self.field_index += 1

    def add_y_from_instrument(self, instrument: Union[PyvisaInstrument, DaqControl]):
        for y_name, index in instrument.y_indexes.items():
            self.add_y(y_name, instrument, instrument.y_values[index])

    def receive_y(self, variable: str):
        y_index = self.y_indexes[variable]
        value = self.instrument_list[y_index].send_y(variable)
        self.value_list[y_index] = value

    def print_ys(self):
        for y_name, value in zip(self.variable_name_list, self.value_list):
            print('y_channel {}: value: {}'.format(y_name, value))

    def get_single_value(self, name: str):
        index = self.y_indexes[name]
        return self.value_list[index]

    def get_values(self):
        return np.array(self.value_list).reshape(-1)

    def get_names(self):
        return self.variable_name_list

    def get_instrument(self, name: str):
        index = self.y_indexes[name]
        return self.instrument_list[index]


class XChannelCollection:

    def __init__(self):
        self.x_index = 0
        self.x_indexes = {}
        self.variable_name_list = []
        self.instrument_list = []
        self.value_list = []

    def add_x(self, variable: str, instrument: Union[PyvisaInstrument, DaqControl], value: float):
        self.x_indexes[variable] = self.x_index
        self.variable_name_list.append(variable)
        self.instrument_list.append(instrument)
        self.value_list.append(value)
        self.x_index += 1

    def add_x_from_instrument(self, instrument: Union[PyvisaInstrument, DaqControl]):
        for x_name, index in instrument.x_indexes.items():
            self.add_x(x_name, instrument, instrument.x_values[index])

    def send_x(self, variable: str, value: float):
        x_index = self.x_indexes[variable]
        self.value_list[x_index] = value
        self.instrument_list[x_index].receive_x(variable, value)

    def print_xs(self):
        for x_name, value in zip(self.variable_name_list, self.value_list):
            print('x_channel {}: value: {}'.format(x_name, value))

    def get_single_value(self, name: str):
        index = self.x_indexes[name]
        return self.value_list[index]

    def get_values(self):
        return np.array(self.value_list).reshape(-1)

    def get_names(self):
        return self.variable_name_list

    def get_instrument(self, name: str) -> Union[PyvisaInstrument, DaqControl]:
        index = self.x_indexes[name]
        return self.instrument_list[index]


class IVSetup:

    def __init__(self, instrument_list: list):
        self.instrument_list = instrument_list

        self.y_channel_collection = YChannelCollection()
        for instrument in instrument_list:
            self.y_channel_collection.add_y_from_instrument(instrument)

        self.x_channel_collection = XChannelCollection()
        for instrument in instrument_list:
            self.x_channel_collection.add_x_from_instrument(instrument)

        self.report_status()

    def report_status(self):
        self.x_channel_collection.print_xs()
        self.y_channel_collection.print_ys()

    def update_xs(self, list_of_xs: list, list_of_values: list):
        for name, value in zip(list_of_xs, list_of_values):
            self.x_channel_collection.send_x(name, value)

    def update_ys(self, list_of_ys: list):
        for name in list_of_ys:
            self.y_channel_collection.receive_y(name)

    def get_x_values(self, list_of_x_names: list):
        lst = []
        for x_name in list_of_x_names:
            lst.append(self.get_single_x_value(x_name))
        return np.array(lst).reshape(-1)

    def get_single_x_value(self, x_name: str):
        return self.x_channel_collection.get_single_value(x_name)

    def get_x_names(self):
        return self.x_channel_collection.get_names()

    def get_y_values(self, list_of_y_names: list):
        lst = []
        for y_name in list_of_y_names:
            lst.append(self.get_single_y_value(y_name))
        return np.array(lst).reshape(-1)

    def get_single_y_value(self, y_name: str):
        return self.y_channel_collection.get_single_value(y_name)

    def get_y_names(self):
        return self.y_channel_collection.get_names()

    def create_1d_sweep(self, sample_name: str, exp_name: str, sweep_x_names: Union[str, list],
                        sweep_y_names: Union[str, list]):
        return OneDSweep(sample_name, exp_name, sweep_x_names, sweep_y_names, self)

    def x_goto(self, x_name: str, target: float, delta: float, delay: float, print_steps: bool=False):
        start = self.get_single_x_value(x_name)

        if delta == 0 or delta is None:
            steps = 2
        else:
            if (target - start) * delta < 0:
                delta = - delta
            steps = (target - start)/delta + 1
            if steps <= 1:
                steps += 1

        y_names = ['measured_' + x_name]
        # Force-include leakage/current channel that matches the X name
        y_names.append(x_name + '_leakage')

        sweep = self.create_1d_sweep('test', 'test', x_name, y_names)
        # enable per-step printing if requested
        if hasattr(sweep, 'set_print'):
            sweep.set_print(print_steps)
        sweep.set_sweep(start, target, int(steps), delay, [1]*len(y_names))
        sweep.trigger_all()


class OneDSweep:

    def __init__(self, sample_name: str, exp_name: str, sweep_x_names: Union[str, list],
                 sweep_y_names: Union[str, list],  iv_setup: IVSetup):
        self.sample_name = sample_name
        self.exp_name = exp_name
        self.print_steps = False

        if isinstance(sweep_x_names, str):
            sweep_x_names = [sweep_x_names]
        if isinstance(sweep_y_names, str):
            sweep_y_names = [sweep_y_names]
        self.sweep_x_names = sweep_x_names
        self.sweep_y_names = sweep_y_names
        self.list_of_variable_names = self.sweep_x_names.copy()
        self.list_of_variable_names.extend(self.sweep_y_names)

        self.iv_setup = iv_setup

        self.instrument_set = set()
        for y_name in self.sweep_y_names:
            self.instrument_set.add(self.iv_setup.y_channel_collection.get_instrument(y_name))
        for x_name in self.sweep_x_names:
            self.instrument_set.add(self.iv_setup.x_channel_collection.get_instrument(x_name))

        self.delay = None
        self.y_factors = None
        self.sweep_x_values = None
        self.total_triggers = None
        self.current_trigger = None

    def set_print(self, print_steps: bool):
        self.print_steps = bool(print_steps)

    def set_sweep(self, x_starts: Union[list, np.ndarray, float], x_ends: Union[list, np.ndarray, float], x_steps: int,
                  delay: float, y_factors: Union[list, np.ndarray, float]):

        x_starts = np.array(x_starts).reshape(-1)
        x_ends = np.array(x_ends).reshape(-1)
        y_factors = np.array(y_factors).reshape(-1)
        if len(x_starts) != len(x_ends) or len(self.sweep_x_names) != len(x_ends):
            print('x dimensions do not match')
            return
        if len(y_factors) != len(self.sweep_y_names):
            print('y dimensions do not match')
            return

        sweep_x_values = np.linspace(x_starts, x_ends, x_steps)

        self.sweep_x_values = sweep_x_values
        self.total_triggers = x_steps

        self.delay = delay
        self.y_factors = y_factors
        self.current_trigger = 0

        '''self.storage = data_collection.OneDSweepData(self.sample_name, self.exp_name, x_steps,
                                                     self.list_of_variable_names, self.plot_x, self.plot_y, plot, save)'''

        print('variable: {}'.format(self.sweep_x_names))
        print('start: {}'.format(x_starts))
        print('end: {}'.format(x_ends))
        print('steps: {}'.format(x_steps))

    def trigger(self):
        if self.current_trigger >= self.total_triggers:
            return

        x_values = self.sweep_x_values[self.current_trigger]

        self.iv_setup.update_xs(self.sweep_x_names, x_values)
        for instrument in self.instrument_set:
            instrument.write_x()
        time.sleep(self.delay)
        for instrument in self.instrument_set:
            instrument.read_y()

        # Update Y channels and fetch values
        self.iv_setup.update_ys(self.sweep_y_names)
        factors = self.y_factors if self.y_factors is not None else 1.0
        y_values = self.iv_setup.get_y_values(self.sweep_y_names) * factors
        data = np.append(x_values, y_values)

        # Optional per-step printing of voltage and current (if available)
        if getattr(self, "print_steps", False):
            y_map = {name: val for name, val in zip(self.sweep_y_names, y_values)}
            measured_names = [n for n in self.sweep_y_names if n.startswith('measured_')]
            leakage_names  = [n for n in self.sweep_y_names if n.endswith('_leakage')]

            # pretty x value
            try:
                import numpy as _np
                x_disp = float(_np.array(x_values).reshape(-1)[0])
            except Exception:
                x_disp = x_values

            # choose values if present
            v_val = y_map.get(measured_names[0], None) if measured_names else None
            i_val = y_map.get(leakage_names[0], None) if leakage_names else None

            step_idx = self.current_trigger + 1
            total = self.total_triggers
            xlab = self.sweep_x_names[0] if self.sweep_x_names else "X"

            if v_val is not None and i_val is not None:
                print(f"[{step_idx}/{total}] {xlab}={x_disp:.6f} V | measured={v_val:.6f} V | current={(i_val*1e6):.3f} uA")
            elif v_val is not None:
                print(f"[{step_idx}/{total}] {xlab}={x_disp:.6f} V | measured={v_val:.6f} V")
            else:
                print(f"[{step_idx}/{total}] {xlab}={x_disp} | y={y_values}")

        self.current_trigger += 1
        return np.copy(data)


    def trigger_all(self):
        for t in range(self.total_triggers):
            self.trigger()

class MagnetPowerSupplyControl(PyvisaInstrument):
    def __init__(self, address: str, name: str, termination: str, rm: pyvisa.ResourceManager):
        super().__init__(address, name, termination, rm)
        self.type = 'attodry1000'
        self.delay = 0.4
        self.connect()
        self.get_identity()
        self.remote()
        self.set_unit()
        self.get_unit()
        self.get_magnetfield()
        self.get_magnetsweeprate() 
        self.heaterstatus = None
        self.target_high_limit = 0.0
        self.target_low_limit = 0.0
        self.current_field = None
        # 90 KGauss
        self.safe_magnetfields = 90
        self.KGausstoAmpera = 2.0328
        self.set_high_sweeplimit(self.target_high_limit)
        self.set_low_sweeplimit(self.target_low_limit)
    def connect(self):
        self.my_instr = self.rm.open_resource(self.address, timeout=self.timeout)
        # self.my_instr.read_termination = self.termination
        # self.my_instr.write_termination = self.termination
        # self.write('REMOTE',print_command= True)
        self.my_instr.read_termination = '\r\n'
        self.my_instr.write_termination = '\r\n'

    def get_identity(self):
        self.query('*IDN?',print_response=True)
        self.read(print_response=True)
# current A in the leads
    def get_magnetfield(self):
        self.query('IMAG?', print_response=True)
        read = self.read(print_response=True)
        field = read.split('kG',1)[0]
        return np.array(field).astype(float)

    
    def get_magnetsweeprate(self):
        query_list = [0,1,2]
        for query in query_list:
            self.query('RATE? {}'.format(query),print_response=True)
            read = self.read(print_response=True)
    
    def remote(self):
        self.write('REMOTE')
        self.read(print_response=True)
    
    def get_unit(self):
        self.write('UNITS?')
        time.sleep(0.1)
        # read = self.read(print_response=True)
        response = self.read()
        print(response)
        response = self.read()
        time.sleep(0.1)
        print(response)

    def set_unit(self):
        self.write('UNITS {}'.format('G'),print_command=True)
        self.read()
        print('x')

    def get_heaterstatus(self):
        self.query('PSHTR?',print_response=True)
        # query returns 1 if the switch heater is ON or 0 if the switch heater is OFF
        read = self.read(print_response=True) 
        return np.array(read).astype(int)
    
    def turnon_heater(self):
        heater = self.get_heaterstatus()
        if heater == 0:
            self.write('PSHTR ON',print_command=True)
            self.read()
            print('pausing 60s')
            time.sleep(60)
        else:
            raise KeyError('Heater already turned on!')
    
    def turnoff_heater(self):
        heater = self.get_heaterstatus()
        if heater == 1:
            self.write('PSHTR OFF',print_command=True)
            self.read()
            print('pausing 120s')
            time.sleep(120)
        else:
            raise KeyError('Heater already turned off!')

    
    def get_low_sweeplimit(self):
        self.query('LLIM?',print_command=True)
        read = self.read(print_response= True)
        return read

    def get_high_sweeplimit(self):
        self.query('ULIM?',print_command=True)
        read = self.read(print_response= True)     
        return read
    
    def set_low_sweeplimit(self,lowlimit):
        if (abs(lowlimit)-self.safe_magnetfields) < 0.001:
            self.target_low_limit = lowlimit
            self.write('LLIM {:.4f}'.format(lowlimit),print_command= True)
            self.read()
        else:
            raise ValueError('Exceeds maximum field!')


    def set_high_sweeplimit(self,highlimit):
        if (abs(highlimit)-self.safe_magnetfields) < 0.001:
            self.target_high_limit = highlimit
            self.write('ULIM {:.4f}'.format(highlimit),print_command= True)
            self.read()
        else:  
            raise ValueError('Exceeds maximum field!')            

    # def set_sweeplimit(self, targetlimit):
        

    def get_sweep_mode(self):
        self.query('SWEEP?',print_command=True)
        read = self.read(print_response= True)
        return read

    def start_sweep(self,mode):
        # Parameter Range: UP, DOWN, PAUSE, or ZERO
        # self.read()
        if self.get_heaterstatus() == 0:
            raise KeyError('Heater is off!')
        else:
            # self.get_sweep_mode()
            if mode == 'UP':
                target = self.target_high_limit   
            elif mode == 'ZERO':
                target = 0.0
            elif mode == 'DOWN':
                target = self.target_low_limit
            else:
                self.write('SWEEP {}'.format(mode),print_command=True)
                raise KeyError('Magnet pausing')
            self.write('SWEEP {}'.format(mode),print_command=True)
            self.read()
            self.get_sweep_mode()
            while True:
                time.sleep(1)
                epsilon = 0.0005
                actual_field = self.get_magnetfield()
                print(target, actual_field)
                if abs(target - actual_field) <= epsilon:
                    time.sleep(10)
                    break
            self.write('SWEEP PAUSE',print_command=True)
            print('Pause at target field:',self.get_magnetfield(),'kG')

                
    def sweep_to_target(self, mode, highlimit=0, lowlimit=0):
        if highlimit < lowlimit:
            raise KeyError('Highlimit < lowlimit!')
        else:
            self.set_high_sweeplimit(highlimit)
            self.set_low_sweeplimit(lowlimit)
            if mode == 'UP':
                self.start_sweep('UP')
            elif mode == "DOWN":
                self.start_sweep('DOWN')

    
    def unsync_start_sweep(self, mode):
        if self.get_heaterstatus() == 0:
            raise KeyError('Heater is off!')
        else:
            # self.get_sweep_mode()
            if mode == 'UP':
                target = self.target_high_limit   
            elif mode == 'ZERO':
                target = 0.0
            elif mode == 'DOWN':
                target = self.target_low_limit
            else:
                self.write('SWEEP {}'.format(mode),print_command=True)
                raise KeyError('Magnet pausing')
            self.write('SWEEP {}'.format(mode),print_command=True)
            self.read()

           
            

    
        
    

        



'''
class OneDSweep:

    def __init__(self, sweep_x_names, sweep_y_names, dict_all_x_channels, dict_all_y_channels, plot_x_name,
                 plot_y_name, sample_name):
        self.sweep_x_channels = []
        self.sweep_y_channels = []
        self.all_x_channels = dict_all_x_channels.values()
        self.sweep_instruments = []
        self.plot_x_name = plot_x_name
        self.plot_y_name = plot_y_name
        self.x_header = []
        for x_channel in self.all_x_channels:
            self.x_header.append(x_channel.name)
        self.y_header = sweep_y_names
        self.plot_x_index = self.x_header.index(plot_x_name)
        self.plot_y_index = self.y_header.index(plot_y_name)
        for x_name in sweep_x_names:
            self.sweep_x_channels.append(dict_all_x_channels[x_name])
        for y_name in sweep_y_names:
            self.sweep_y_channels.append(dict_all_y_channels[y_name])
        self.__get_sweep_instruments()
        self.sample_name = sample_name
        self.file_number = -1

    def __get_sweep_instruments(self):
        for xchannel in self.sweep_x_channels:
            if xchannel.x_instrument not in self.sweep_instruments:
                self.sweep_instruments.append(xchannel.x_instrument)
            if xchannel.y_instrument not in self.sweep_instruments:
                self.sweep_instruments.append(xchannel.y_instrument)
        for ychannel in self.sweep_y_channels:
            if ychannel.y_instrument not in self.sweep_instruments:
                self.sweep_instruments.append(ychannel.y_instrument)

    def one_d_sweep(self, list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name, save_data=True, plot=True, timer=True):
        # values for each channel should be passed as a row vector
        x_numbers = len(self.sweep_x_channels)
        x_array = np.array(list_of_x_values).reshape([x_numbers, -1]).T
        self.xs_goto(x_array[0, :], ramp_steps, delay)
        rows, columns = x_array.shape
        y_amp_rates = np.array(y_amp_rates)
        sweep_x_data = np.empty([rows, len(self.x_header)])
        sweep_y_data = np.empty([rows, len(self.y_header)])
        sweep_x_data[:, :] = np.nan
        sweep_y_data[:, :] = np.nan
        if plot:
            data_plot = plt.subplot(1, 1, 1)
        for i in range(rows):
            if timer:
                t1 = time.time()
            values_to_update = x_array[i]
            self.__update_x(values_to_update)
            self.__write_x()
            time.sleep(delay)
            self.__read_y()
            self.__update_y()
            x_data = self.__collect_x()
            y_data = self.__collect_y()/y_amp_rates
            sweep_x_data[i] = x_data
            sweep_y_data[i] = y_data
            if plot:
                plt.cla()
                data_plot.plot(sweep_x_data[:, self.plot_x_index], sweep_y_data[:, self.plot_y_index])
                plt.pause(0.001)
            if timer:
                t2 = time.time()
                print('{:.4f} s per frame'.format(t2-t1))
        data = np.append(sweep_x_data, sweep_y_data, axis=1)
        if save_data:
            self.__save_data(data, experiment_name)

    def xs_goto(self, list_of_targets, list_of_steps, delay):
        for (xchannel, target, step) in zip (self.sweep_x_channels, list_of_targets, list_of_steps):
            xchannel.x_goto(target, step, delay)
           

    def __save_data(self, data, experiment_name):
        header = self.x_header.copy()
        header.extend(self.y_header)
        df = pd.DataFrame(data, columns=header)
        while True:
            self.file_number += 1
            file_name = '{}_{}_{:0>3d}'.format(self.sample_name, experiment_name, self.file_number)
            csv_name = file_name + '.csv'
            if not os.path.exists(csv_name):
                df.to_csv(csv_name)
                plt.title(file_name)
                plt.xlabel(self.plot_x_name)
                plt.ylabel(self.plot_y_name)
                plt.savefig(file_name)
                plt.close()
                break

    def __update_x(self, values_to_update):
        for xchannel, x in zip(self.sweep_x_channels, values_to_update):
            xchannel.update_x(x)

    def __write_x(self):
        for instrument in self.sweep_instruments:
            instrument.write_x()

    def __read_y(self):
        for instrument in self.sweep_instruments:
            instrument.read_y()

    def __update_y(self):
        for ychannel in self.sweep_y_channels:
            ychannel.update_y()
        for xchannel in self.sweep_x_channels:
            xchannel.update_y()

    def __collect_x(self):
        data = []
        for xchannel in self.all_x_channels:
            data.append(xchannel.x_value)
        return np.array(data)

    def __collect_y(self):
        data = []
        for ychannel in self.sweep_y_channels:
            data.append(ychannel.y_value)
        return np.array(data)
'''

class TwoDSweep:
    def __init__(self, outer_x_names, inner_x_names, sweep_y_names,
                 dict_all_x_channels, dict_all_y_channels, plot_x_name,
                 plot_y_name, sample_name):

        self.outer_x_names = outer_x_names
        self.inner_x_names = inner_x_names
        self.outer_sweep = OneDSweep(outer_x_names, sweep_y_names, dict_all_x_channels, dict_all_y_channels,
                                     plot_x_name, plot_y_name, sample_name)

        self.inner_sweep = OneDSweep(inner_x_names, sweep_y_names, dict_all_x_channels, dict_all_y_channels,
                                     plot_x_name, plot_y_name, sample_name)

    def two_d_sweep(self, list_of_outer_values, outer_ramp_steps, list_of_inner_values, inner_ramp_steps,
                    delay, y_amp_rates, experiment_name):
        outer_x_numbers = len(self.outer_x_names)
        outer_x_array = np.array(list_of_outer_values).reshape(outer_x_numbers, -1).T
        rows, columns = outer_x_array.shape
        inner_x_numbers = len(self.inner_x_names)
        inner_x_array = np.array(list_of_inner_values).reshape(inner_x_numbers, -1).T
        for i in range(rows):
            self.outer_sweep.xs_goto(outer_x_array[i], outer_ramp_steps, delay)
            self.inner_sweep.xs_goto(inner_x_array[0], inner_ramp_steps, delay)
            self.inner_sweep.one_d_sweep(list_of_inner_values, inner_ramp_steps, delay, y_amp_rates, experiment_name)

    def inner_1d_sweep(self, list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name, save_data=True):
        self.inner_sweep.one_d_sweep(list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name,
                                     save_data=save_data)

    def outer_1d_sweep(self, list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name, save_data=True):
        self.outer_sweep.one_d_sweep(list_of_x_values, ramp_steps, delay, y_amp_rates, experiment_name,
                                     save_data=save_data)


if __name__ == '__main__':
    # keithley = KeithControl('GPIB1::6::INSTR', 'GPIB6', 'Vbg', pyvisa.ResourceManager())
    # keithley.volt_step(-0.1)
    MSP = MagnetPowerSupplyControl('ASRL4::INSTR','MSP','\r\n',pyvisa.ResourceManager())
 
    # MSP.get_heaterstatus()
    # MSP.get_high_sweeplimit()
    # MSP.get_low_sweeplimit()
    # MSP.set_high_sweeplimit(0.2)
    # print('---')
    # MSP.get_high_sweeplimit()
    # MSP.get_low_sweeplimit()

    MSP.get_unit()
    print('---')
    # time.sleep(1)
    # MSP.start_sweep('UP')
    
















