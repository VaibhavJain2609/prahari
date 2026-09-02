# prahari-proto

Generated protobuf and gRPC stubs. **Nothing in `src/prahari/v1/` is committed** —
the two `__init__.py` files are, the `*_pb2*.py` files are not.

Regenerate:

```
make proto
```

If an import of `prahari.v1.events_pb2` fails on a fresh clone, that is the
expected state: run `make proto`. The contract is `proto/prahari/v1/*.proto`;
these files are a build artifact of it.
