# Linear regression on the Boston housing data

Install the libraries:

```bash
pip install scikit-learn pandas
```

Load the dataset and fit a model:

```python
from sklearn.datasets import load_boston
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split

X, y = load_boston(return_X_y=True)
X_train, X_test, y_train, y_test = train_test_split(X, y, random_state=0)
model = LinearRegression().fit(X_train, y_train)
print("R^2:", round(model.score(X_test, y_test), 3))
```
