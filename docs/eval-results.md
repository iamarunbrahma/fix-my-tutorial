# Evaluation results

| fixture | expected | verdict | category | rounds | seconds | cost $ | tavily | note |
|---|---|---|---|---|---|---|---|---|
| js_chalk_require | fixed | fixed | removed_api | 1 | 39 | 0.0102 | 0 |  |
| js_express_wildcard | fixed | fixed | unknown | 1 | 128 | 0.0122 | 0 |  |
| js_faker | fixed | fixed | renamed_package | 1 | 43 | 0.0084 | 3 |  |
| js_node_fetch_require | fixed | fixed | removed_api | 1 | 26 | 0.0071 | 0 |  |
| js_still_works | still_works | still_works |  | 0 | 5 | 0.0011 | 0 |  |
| py_flask_before_first_request | fixed | fixed | removed_api | 1 | 86 | 0.0123 | 4 |  |
| py_langchain_llms | fixed | fixed | removed_api | 1 | 99 | 0.0120 | 3 |  |
| py_load_boston | fixed | fixed | removed_api | 1 | 105 | 0.0145 | 2 |  |
| py_np_float | fixed | fixed | removed_api | 1 | 35 | 0.0079 | 1 |  |
| py_openai_v0 | fixed | fixed | removed_api | 1 | 49 | 0.0160 | 2 |  |
| py_pandas_append | fixed | fixed | removed_api | 1 | 60 | 0.0105 | 1 |  |
| py_pandas_iteritems | fixed | fixed | removed_api | 1 | 41 | 0.0049 | 2 |  |
| py_seaborn_style | fixed | fixed | environment | 1 | 82 | 0.0106 | 3 |  |
| py_sklearn_ohe_sparse | fixed | fixed | removed_api | 1 | 76 | 0.0113 | 3 |  |
| py_still_works | still_works | still_works |  | 0 | 7 | 0.0014 | 0 |  |

**fixed 13/13 rot fixtures; 0 control(s) changed**
