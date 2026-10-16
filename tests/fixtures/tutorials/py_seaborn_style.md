# Prettier charts with the seaborn style

```bash
pip install matplotlib
```

```python
import matplotlib.pyplot as plt

plt.style.use("seaborn")
plt.plot([1, 2, 3], [2, 4, 9])
plt.savefig("chart.png")
print("saved chart.png")
```
