from ctypes import c_double, c_int16, c_bool, byref, create_string_buffer, c_char_p
try:
    # This imports the class from the TLPMX.py file you uploaded
    from TLPMX import TLPMX, TLPM_DEFAULT_CHANNEL
except ImportError:
    TLPMX = None
    TLPM_DEFAULT_CHANNEL = 1

class ThorlabsPM100D_Wrapper:
    def __init__(self, resource_name_str):
        if TLPMX is None:
            raise ImportError("TLPMX.py is missing! Please put it in the project root.")

        # --- STEP 1: PREPARE THE NAME ---
        # Strip whitespace
        clean_name = resource_name_str.strip()
        print(f"DEBUG: Connecting to '{clean_name}'")
        
        # CRITICAL: Convert Python String -> C Byte Buffer
        # The driver will crash or fail if we don't do this
        r_name_buffer = create_string_buffer(clean_name.encode('ascii'))

        # --- STEP 2: OPEN CONNECTION ---
        # We pass the buffer to the TLPMX constructor
        try:
            self.inst = TLPMX(resourceName=r_name_buffer, IDQuery=c_bool(True), resetDevice=c_bool(True))
        except Exception as e:
            # If it fails, the device might be locked.
            raise Exception(f"Connection Failed: {e}. Try unplugging USB cable.")

        # --- STEP 3: CONFIGURE ---
        try:
            # Set Wavelength to 730nm (default)
            self.inst.setWavelength(c_double(730), TLPM_DEFAULT_CHANNEL)
            # Enable Auto Range
            self.inst.setPowerAutoRange(c_int16(1), TLPM_DEFAULT_CHANNEL)
            # Set Unit to Watts (0)
            self.inst.setPowerUnit(c_int16(0), TLPM_DEFAULT_CHANNEL)
        except Exception as e:
            print(f"Warning: PM100D Config failed ({e}), but connection is active.")

    def get_power(self):
        val = c_double()
        try:
            # Measure Power
            self.inst.measPower(byref(val), TLPM_DEFAULT_CHANNEL)
            return val.value
        except:
            return float('nan')
        
    def configure_wavelength(self, nm):
        """Sets the wavelength correction (in nm)."""
        try:
            # TLPMX requires c_double for the value
            val = c_double(float(nm))
            self.inst.setWavelength(val, TLPM_DEFAULT_CHANNEL)
            print(f"DEBUG: Wavelength set to {nm} nm")
        except Exception as e:
            print(f"Error setting wavelength: {e}")
            raise e
        
    def close(self):
        try:
            if self.inst:
                self.inst.close()
        except:
            pass