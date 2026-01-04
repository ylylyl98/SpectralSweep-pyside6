import pylablib as pll
from pylablib.devices import Thorlabs
import time

class LinearStage:
    def __init__(self, port):
        # "COM5" passed from UI
        self.stage = Thorlabs.ElliptecMotor(port) 
        
    def move_to(self, position_mm):
        # Assumes elliptec stage uses steps or similar, 
        # You might need conversion factors depending on specific model
        # For this example, we assume direct move or simplistic step count
        self.stage.move_to(position_mm) 
        # Elliptec moves are often blocking, but adding safe sleep is good
        time.sleep(0.5)
        
    def get_position(self):
        return self.stage.get_position()

    def close(self):
        self.stage.close()