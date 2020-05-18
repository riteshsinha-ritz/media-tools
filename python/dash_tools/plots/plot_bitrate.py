# libraries
import numpy as np
import matplotlib.pyplot as plt

# Total Bitrate utility
height = [1303.25/48.0 , 1504.05/48.0, 1568.1/48.0, 1568.1/48.0, 1012.94/48.0]
bars = ('SimpleABR', 'BOLA', 'BBA0', 'BBA2', 'Pensieve')
y_pos = np.arange(len(bars))

plt.bar(y_pos, height, color=['yellow', 'red', 'green', 'blue', 'cyan'], edgecolor='black')
plt.xticks(y_pos, bars)
plt.ylabel("Average value")
plt.xlabel("Bitrate utility")
plt.show()

