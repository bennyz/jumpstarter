"""Transport adapter for jmp-micro exporters running on microcontrollers.

Speaks the jmp-micro wire protocol (4-byte big-endian length + JSON) over TCP,
and translates to the same stub interface that client_from_channel expects.
"""

import asyncio
import json
import logging
import struct
from collections import OrderedDict, defaultdict
from contextlib import ExitStack, asynccontextmanager
from graphlib import TopologicalSorter
from uuid import UUID, uuid4

import grpc
from anyio.from_thread import BlockingPortal
from google.protobuf import json_format, struct_pb2
from grpc.aio import AioRpcError
from jumpstarter_protocol import jumpstarter_pb2

from jumpstarter.client.base import StubDriverClient
from jumpstarter.common.exceptions import MissingDriverError
from jumpstarter.common.importlib import import_class

logger = logging.getLogger(__name__)


class MicroExporterStub:
    """Stub that speaks the jmp-micro wire protocol over TCP.

    Implements the same method signatures as MultipathExporterStub
    (GetReport, DriverCall, etc.) so it can be used as the stub
    parameter when constructing DriverClient instances.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._id_counter = 0
        self._uuid_to_driver: dict[str, str] = {}
        self._root_uuid = str(uuid4())

    async def _send_recv(self, message: dict) -> dict:
        """Send a length-prefixed JSON message and read the response."""
        payload = json.dumps(message).encode()
        self._writer.write(struct.pack(">I", len(payload)))
        self._writer.write(payload)
        await self._writer.drain()

        len_buf = await self._reader.readexactly(4)
        resp_len = struct.unpack(">I", len_buf)[0]
        resp_buf = await self._reader.readexactly(resp_len)
        return json.loads(resp_buf)

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def GetReport(self, request):
        """Send 'report' to MCU, synthesize a GetReportResponse."""
        resp = await self._send_recv({"id": self._next_id(), "type": "report"})

        drivers = resp.get("drivers", [])
        reports = []

        # Root report (CompositeClient) — must come first so children
        # can reference it via parent_uuid in client_from_micro.
        reports.append(jumpstarter_pb2.DriverInstanceReport(
            uuid=self._root_uuid,
            labels={
                "jumpstarter.dev/client": "jumpstarter_driver_composite.client.CompositeClient",
                "jumpstarter.dev/name": "micro",
            },
        ))

        # One child report per MCU driver
        for driver in drivers:
            driver_uuid = str(uuid4())
            driver_name = driver["name"]
            methods = driver.get("methods", [])

            self._uuid_to_driver[driver_uuid] = driver_name

            reports.append(jumpstarter_pb2.DriverInstanceReport(
                uuid=driver_uuid,
                parent_uuid=self._root_uuid,
                labels={
                    "jumpstarter.dev/client": "jumpstarter.client.DriverClient",
                    "jumpstarter.dev/name": driver_name,
                },
                methods_description={m: "" for m in methods},
            ))

        return jumpstarter_pb2.GetReportResponse(
            uuid=self._root_uuid,
            reports=reports,
        )

    async def DriverCall(self, request):
        """Map UUID to driver name, send 'call', return DriverCallResponse."""
        driver_name = self._uuid_to_driver.get(request.uuid)
        if not driver_name:
            raise AioRpcError(
                grpc.StatusCode.NOT_FOUND, None, None,
                f"unknown driver uuid: {request.uuid}", None,
            )

        # Convert protobuf Value args to plain Python values
        args = [json_format.MessageToDict(arg) for arg in request.args]

        resp = await self._send_recv({
            "id": self._next_id(),
            "type": "call",
            "driver": driver_name,
            "method": request.method,
            "args": args,
        })

        if "error" in resp:
            raise AioRpcError(
                grpc.StatusCode.INTERNAL, None, None,
                resp["error"], None,
            )

        result_value = json_format.ParseDict(
            resp.get("result"), struct_pb2.Value(),
        )
        return jumpstarter_pb2.DriverCallResponse(
            uuid=request.uuid,
            result=result_value,
        )

    # -- Unsupported operations (MCU doesn't implement these) --

    async def GetStatus(self, request):
        raise AioRpcError(
            grpc.StatusCode.UNIMPLEMENTED, None, None,
            "not supported on MCU", None,
        )

    async def EndSession(self, request):
        raise AioRpcError(
            grpc.StatusCode.UNIMPLEMENTED, None, None,
            "not supported on MCU", None,
        )

    def LogStream(self, request):
        raise AioRpcError(
            grpc.StatusCode.UNIMPLEMENTED, None, None,
            "not supported on MCU", None,
        )

    def StreamingDriverCall(self, request):
        raise AioRpcError(
            grpc.StatusCode.UNIMPLEMENTED, None, None,
            "not supported on MCU", None,
        )

    def Stream(self, **kwargs):
        raise AioRpcError(
            grpc.StatusCode.UNIMPLEMENTED, None, None,
            "not supported on MCU", None,
        )


@asynccontextmanager
async def client_from_micro(
    address: str,
    portal: BlockingPortal,
    stack: ExitStack,
    allow: list[str],
    unsafe: bool,
):
    """Create a DriverClient tree from a jmp-micro exporter at host:port."""
    host, port_str = address.rsplit(":", 1)
    reader, writer = await asyncio.open_connection(host, int(port_str))

    try:
        stub = MicroExporterStub(reader, writer)

        # Same topology-building logic as client_from_channel
        topo = defaultdict(list)
        last_seen = {}
        reports = {}
        clients: OrderedDict = OrderedDict()

        response = await stub.GetReport(None)

        for index, report in enumerate(response.reports):
            topo[index] = []
            last_seen[report.uuid] = index

            if report.parent_uuid != "":
                parent_index = last_seen[report.parent_uuid]
                topo[parent_index].append(index)

            reports[index] = report

        for index in TopologicalSorter(topo).static_order():
            report = reports[index]

            try:
                client_class = import_class(
                    report.labels["jumpstarter.dev/client"], allow, unsafe,
                )
            except MissingDriverError:
                client_class = StubDriverClient

            client = client_class(
                uuid=UUID(report.uuid),
                labels=report.labels,
                stub=stub,
                portal=portal,
                stack=stack.enter_context(ExitStack()),
                children={
                    reports[k].labels["jumpstarter.dev/name"]: clients[k]
                    for k in topo[index]
                },
                description=getattr(report, "description", None) or None,
                methods_description=getattr(report, "methods_description", {}) or {},
            )

            clients[index] = client

        yield clients.popitem(last=True)[1]
    finally:
        writer.close()
        await writer.wait_closed()
