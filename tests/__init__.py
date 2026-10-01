import os
import sys

# the app's modules live in src/ and import each other by bare name, so src/ goes first on the path for every test
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
