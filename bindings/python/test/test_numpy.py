# Copyright 2021-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# from datetime import datetime, timedelta
import unittest
from test import client_context
from test.utils import AllowListEventListener
from unittest import mock

import numpy as np
from bson import Decimal128, ObjectId
from pyarrow import int32, int64
from pymongo import DESCENDING, WriteConcern
from pymongo.collection import Collection
from pymongoarrow.api import Schema, aggregate_numpy_all, find_numpy_all, write
from pymongoarrow.errors import ArrowWriteError
from pymongoarrow.types import (
    _TYPE_NORMALIZER_FACTORY,
    Decimal128StringType,
    ObjectIdType,
)


class NumpyTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not client_context.connected:
            raise unittest.SkipTest("cannot connect to MongoDB")
        cls.cmd_listener = AllowListEventListener("find", "aggregate")
        cls.getmore_listener = AllowListEventListener("getMore")
        cls.client = client_context.get_client(
            event_listeners=[cls.getmore_listener, cls.cmd_listener]
        )
        cls.schema = {}

    def assert_numpy_equal(self, actual, expected):
        self.assertIsInstance(actual, dict)
        for field in expected:
            # workaround np.nan == np.nan evaluating to False
            a = np.nan_to_num(actual[field])
            e = np.nan_to_num(expected[field])
            np.testing.assert_array_equal(a, e)
            self.assertEqual(actual[field].dtype, expected[field].dtype)


class TestExplicitNumPyApi(NumpyTestBase):
    @classmethod
    def setUpClass(cls):
        NumpyTestBase.setUpClass()
        cls.schema = Schema({"_id": int32(), "data": int64()})
        cls.coll = cls.client.pymongoarrow_test.get_collection(
            "test", write_concern=WriteConcern(w="majority")
        )

    def setUp(self):
        self.coll.drop()
        self.coll.insert_many(
            [{"_id": 1, "data": 10}, {"_id": 2, "data": 20}, {"_id": 3, "data": 30}, {"_id": 4}]
        )
        self.cmd_listener.reset()
        self.getmore_listener.reset()

    def test_find_simple(self):
        expected = {
            "_id": np.array([1, 2, 3, 4], dtype=np.int32),
            # integer arrays with NaNs are given dtype float64 by NumPy
            "data": np.array([10, 20, 30, np.nan], dtype=np.float64),
        }

        actual = find_numpy_all(self.coll, {}, schema=self.schema)
        self.assert_numpy_equal(actual, expected)

        expected = {
            "_id": np.array([4, 3], dtype=np.int32),
            "data": np.array([np.nan, 30], dtype=np.float64),
        }
        actual = find_numpy_all(
            self.coll, {"_id": {"$gt": 2}}, schema=self.schema, sort=[("_id", DESCENDING)]
        )
        self.assert_numpy_equal(actual, expected)

        find_cmd = self.cmd_listener.results["started"][-1]
        self.assertEqual(find_cmd.command_name, "find")
        self.assertEqual(find_cmd.command["projection"], {"_id": True, "data": True})

    def test_aggregate_simple(self):
        expected = {
            "_id": np.array([1, 2, 3, 4], dtype=np.int32),
            "data": np.array([20, 40, 60, None], dtype=np.float64),
        }
        projection = {"_id": True, "data": {"$multiply": [2, "$data"]}}
        actual = aggregate_numpy_all(self.coll, [{"$project": projection}], schema=self.schema)
        self.assert_numpy_equal(actual, expected)

        agg_cmd = self.cmd_listener.results["started"][-1]
        self.assertEqual(agg_cmd.command_name, "aggregate")
        assert len(agg_cmd.command["pipeline"]) == 2
        self.assertEqual(agg_cmd.command["pipeline"][0]["$project"], projection)
        self.assertEqual(agg_cmd.command["pipeline"][1]["$project"], {"_id": True, "data": True})

    def round_trip(self, data, schema, coll=None):
        if coll is None:
            coll = self.coll
        coll.drop()
        res = write(self.coll, data)
        self.assertEqual(len(list(data.values())[0]), res.raw_result["insertedCount"])
        self.assert_numpy_equal(find_numpy_all(coll, {}, schema=schema), data)
        return res

    def schemafied_ndarray_dict(self, dict, schema):
        ret = {}
        for k, v in dict.items():
            ret[k] = np.array(v, dtype=schema[k])
        return ret

    def test_write_error(self):
        schema = {"_id": "int32", "data": "int64"}
        length = 10001
        data = {"_id": [i for i in range(length)] * 2, "data": [i * 2 for i in range(length)] * 2}
        data = self.schemafied_ndarray_dict(data, schema)
        with self.assertRaises(ArrowWriteError):
            try:
                self.round_trip(data, Schema({"_id": int32(), "data": int64()}))
            except ArrowWriteError as awe:
                self.assertEqual(
                    10001, awe.details["writeErrors"][0]["index"], awe.details["nInserted"]
                )
                raise awe

    def test_write_schema_validation(self):
        arrow_schema = {
            k.__name__: v(True)
            for k, v in _TYPE_NORMALIZER_FACTORY.items()
            if k.__name__ not in ("ObjectId", "Decimal128")
        }
        schema = {k: v.to_pandas_dtype() for k, v in arrow_schema.items()}
        schema["str"] = "str"
        schema["datetime"] = "datetime64[ms]"
        data = {
            "Int64": [i for i in range(2)],
            "float": [i for i in range(2)],
            "datetime": [i for i in range(2)],
            "str": [str(i) for i in range(2)],
            "int": [i for i in range(2)],
            "bool": [True, False],
        }
        data = self.schemafied_ndarray_dict(data, schema)
        self.round_trip(
            data,
            Schema(arrow_schema),
        )

        schema = {"_id": "int32", "data": np.ubyte()}
        data = {"_id": [i for i in range(2)], "data": [i for i in range(2)]}
        data = self.schemafied_ndarray_dict(data, schema)
        with self.assertRaises(ValueError):
            self.round_trip(data, Schema({"_id": int32(), "data": np.ubyte()}))

    @mock.patch.object(Collection, "insert_many", side_effect=Collection.insert_many, autospec=True)
    def test_write_batching(self, mock):
        schema = {"_id": "int64"}
        data = {"_id": [i for i in range(100040)]}
        data = self.schemafied_ndarray_dict(data, schema)

        self.round_trip(
            data,
            Schema(
                {
                    "_id": int64(),
                }
            ),
            coll=self.coll,
        )
        self.assertEqual(mock.call_count, 2)

    def test_write_dictionaries(self):
        with self.assertRaisesRegex(
            ValueError, "Invalid tabular data object of type <class 'dict'>"
        ):
            write(self.coll, {"foo": 1})

    def test_string_bool(self):
        schema = {
            "string": "str",
            "bool": "bool",
        }
        data = {
            "string": [str(i) for i in range(2)],
            "bool": [True for _ in range(2)],
        }
        data = self.schemafied_ndarray_dict(data, schema)
        self.round_trip(
            data,
            Schema(
                {
                    "string": str,
                    "bool": bool,
                }
            ),
        )


class TestBSONTypes(NumpyTestBase):
    @classmethod
    def setUpClass(cls):
        NumpyTestBase.setUpClass()
        cls.schema = Schema({"_id": ObjectIdType(), "decimal128": Decimal128StringType()})
        cls.coll = cls.client.pymongoarrow_test.get_collection(
            "test", write_concern=WriteConcern(w="majority")
        )
        cls.oids = [ObjectId() for _ in range(4)]
        cls.decimal_128s = [Decimal128(i) for i in ["1.0", "0.1", "1e-5"]]

    def setUp(self):
        self.coll.drop()
        self.coll.insert_many(
            [
                {"_id": self.oids[0], "decimal128": self.decimal_128s[0]},
                {"_id": self.oids[1], "decimal128": self.decimal_128s[1]},
                {"_id": self.oids[2], "decimal128": self.decimal_128s[2]},
                {"_id": self.oids[3]},
            ]
        )
        self.cmd_listener.reset()
        self.getmore_listener.reset()

    def test_find_decimal128(self):
        decimals = [str(i) for i in self.decimal_128s] + [None]  # type:ignore
        expected = {
            "_id": np.array([i.binary for i in self.oids], dtype=np.object_),
            "decimal128": np.array(decimals),
        }
        actual = find_numpy_all(self.coll, {}, schema=self.schema)
        self.assert_numpy_equal(actual, expected)
