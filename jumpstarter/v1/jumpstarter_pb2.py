# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# NO CHECKED-IN PROTOBUF GENCODE
# source: jumpstarter/v1/jumpstarter.proto
# Protobuf Python Version: 5.28.2
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import runtime_version as _runtime_version
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder
_runtime_version.ValidateProtobufRuntimeVersion(
    _runtime_version.Domain.PUBLIC,
    5,
    28,
    2,
    '',
    'jumpstarter/v1/jumpstarter.proto'
)
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()


from google.protobuf import duration_pb2 as google_dot_protobuf_dot_duration__pb2
from google.protobuf import empty_pb2 as google_dot_protobuf_dot_empty__pb2
from google.protobuf import struct_pb2 as google_dot_protobuf_dot_struct__pb2
from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2
from jumpstarter.v1 import kubernetes_pb2 as jumpstarter_dot_v1_dot_kubernetes__pb2


DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n jumpstarter/v1/jumpstarter.proto\x12\x0ejumpstarter.v1\x1a\x1egoogle/protobuf/duration.proto\x1a\x1bgoogle/protobuf/empty.proto\x1a\x1cgoogle/protobuf/struct.proto\x1a\x1fgoogle/protobuf/timestamp.proto\x1a\x1fjumpstarter/v1/kubernetes.proto\"\xd1\x01\n\x0fRegisterRequest\x12\x43\n\x06labels\x18\x01 \x03(\x0b\x32+.jumpstarter.v1.RegisterRequest.LabelsEntryR\x06labels\x12>\n\x07reports\x18\x02 \x03(\x0b\x32$.jumpstarter.v1.DriverInstanceReportR\x07reports\x1a\x39\n\x0bLabelsEntry\x12\x10\n\x03key\x18\x01 \x01(\tR\x03key\x12\x14\n\x05value\x18\x02 \x01(\tR\x05value:\x02\x38\x01\"\xe5\x01\n\x14\x44riverInstanceReport\x12\x12\n\x04uuid\x18\x01 \x01(\tR\x04uuid\x12$\n\x0bparent_uuid\x18\x02 \x01(\tH\x00R\nparentUuid\x88\x01\x01\x12H\n\x06labels\x18\x03 \x03(\x0b\x32\x30.jumpstarter.v1.DriverInstanceReport.LabelsEntryR\x06labels\x1a\x39\n\x0bLabelsEntry\x12\x10\n\x03key\x18\x01 \x01(\tR\x03key\x12\x14\n\x05value\x18\x02 \x01(\tR\x05value:\x02\x38\x01\x42\x0e\n\x0c_parent_uuid\"&\n\x10RegisterResponse\x12\x12\n\x04uuid\x18\x01 \x01(\tR\x04uuid\"+\n\x11UnregisterRequest\x12\x16\n\x06reason\x18\x02 \x01(\tR\x06reason\"\x14\n\x12UnregisterResponse\".\n\rListenRequest\x12\x1d\n\nlease_name\x18\x01 \x01(\tR\tleaseName\"\\\n\x0eListenResponse\x12\'\n\x0frouter_endpoint\x18\x01 \x01(\tR\x0erouterEndpoint\x12!\n\x0crouter_token\x18\x02 \x01(\tR\x0brouterToken\"\x0f\n\rStatusRequest\"\x91\x01\n\x0eStatusResponse\x12\x16\n\x06leased\x18\x01 \x01(\x08R\x06leased\x12\"\n\nlease_name\x18\x02 \x01(\tH\x00R\tleaseName\x88\x01\x01\x12$\n\x0b\x63lient_name\x18\x03 \x01(\tH\x01R\nclientName\x88\x01\x01\x42\r\n\x0b_lease_nameB\x0e\n\x0c_client_name\",\n\x0b\x44ialRequest\x12\x1d\n\nlease_name\x18\x01 \x01(\tR\tleaseName\"Z\n\x0c\x44ialResponse\x12\'\n\x0frouter_endpoint\x18\x01 \x01(\tR\x0erouterEndpoint\x12!\n\x0crouter_token\x18\x02 \x01(\tR\x0brouterToken\"\xa1\x01\n\x12\x41uditStreamRequest\x12#\n\rexporter_uuid\x18\x01 \x01(\tR\x0c\x65xporterUuid\x12\x30\n\x14\x64river_instance_uuid\x18\x02 \x01(\tR\x12\x64riverInstanceUuid\x12\x1a\n\x08severity\x18\x03 \x01(\tR\x08severity\x12\x18\n\x07message\x18\x04 \x01(\tR\x07message\"\xe9\x01\n\x11GetReportResponse\x12\x12\n\x04uuid\x18\x01 \x01(\tR\x04uuid\x12\x45\n\x06labels\x18\x02 \x03(\x0b\x32-.jumpstarter.v1.GetReportResponse.LabelsEntryR\x06labels\x12>\n\x07reports\x18\x03 \x03(\x0b\x32$.jumpstarter.v1.DriverInstanceReportR\x07reports\x1a\x39\n\x0bLabelsEntry\x12\x10\n\x03key\x18\x01 \x01(\tR\x03key\x12\x14\n\x05value\x18\x02 \x01(\tR\x05value:\x02\x38\x01\"k\n\x11\x44riverCallRequest\x12\x12\n\x04uuid\x18\x01 \x01(\tR\x04uuid\x12\x16\n\x06method\x18\x02 \x01(\tR\x06method\x12*\n\x04\x61rgs\x18\x03 \x03(\x0b\x32\x16.google.protobuf.ValueR\x04\x61rgs\"X\n\x12\x44riverCallResponse\x12\x12\n\x04uuid\x18\x01 \x01(\tR\x04uuid\x12.\n\x06result\x18\x02 \x01(\x0b\x32\x16.google.protobuf.ValueR\x06result\"t\n\x1aStreamingDriverCallRequest\x12\x12\n\x04uuid\x18\x01 \x01(\tR\x04uuid\x12\x16\n\x06method\x18\x02 \x01(\tR\x06method\x12*\n\x04\x61rgs\x18\x03 \x03(\x0b\x32\x16.google.protobuf.ValueR\x04\x61rgs\"a\n\x1bStreamingDriverCallResponse\x12\x12\n\x04uuid\x18\x01 \x01(\tR\x04uuid\x12.\n\x06result\x18\x02 \x01(\x0b\x32\x16.google.protobuf.ValueR\x06result\"]\n\x11LogStreamResponse\x12\x12\n\x04uuid\x18\x01 \x01(\tR\x04uuid\x12\x1a\n\x08severity\x18\x02 \x01(\tR\x08severity\x12\x18\n\x07message\x18\x03 \x01(\tR\x07message\"\x0e\n\x0cResetRequest\"\x0f\n\rResetResponse\"\x9b\x01\n\x14ListExportersRequest\x12H\n\x06labels\x18\x01 \x03(\x0b\x32\x30.jumpstarter.v1.ListExportersRequest.LabelsEntryR\x06labels\x1a\x39\n\x0bLabelsEntry\x12\x10\n\x03key\x18\x01 \x01(\tR\x03key\x12\x14\n\x05value\x18\x02 \x01(\tR\x05value:\x02\x38\x01\"X\n\x15ListExportersResponse\x12?\n\texporters\x18\x01 \x03(\x0b\x32!.jumpstarter.v1.GetReportResponseR\texporters\"(\n\x12GetExporterRequest\x12\x12\n\x04uuid\x18\x01 \x01(\tR\x04uuid\"T\n\x13GetExporterResponse\x12=\n\x08\x65xporter\x18\x01 \x01(\x0b\x32!.jumpstarter.v1.GetReportResponseR\x08\x65xporter\"%\n\x0fGetLeaseRequest\x12\x12\n\x04name\x18\x01 \x01(\tR\x04name\"\x93\x03\n\x10GetLeaseResponse\x12\x35\n\x08\x64uration\x18\x01 \x01(\x0b\x32\x19.google.protobuf.DurationR\x08\x64uration\x12\x39\n\x08selector\x18\x02 \x01(\x0b\x32\x1d.jumpstarter.v1.LabelSelectorR\x08selector\x12>\n\nbegin_time\x18\x03 \x01(\x0b\x32\x1a.google.protobuf.TimestampH\x00R\tbeginTime\x88\x01\x01\x12:\n\x08\x65nd_time\x18\x04 \x01(\x0b\x32\x1a.google.protobuf.TimestampH\x01R\x07\x65ndTime\x88\x01\x01\x12(\n\rexporter_uuid\x18\x05 \x01(\tH\x02R\x0c\x65xporterUuid\x88\x01\x01\x12\x39\n\nconditions\x18\x06 \x03(\x0b\x32\x19.jumpstarter.v1.ConditionR\nconditionsB\r\n\x0b_begin_timeB\x0b\n\t_end_timeB\x10\n\x0e_exporter_uuid\"\x87\x01\n\x13RequestLeaseRequest\x12\x35\n\x08\x64uration\x18\x01 \x01(\x0b\x32\x19.google.protobuf.DurationR\x08\x64uration\x12\x39\n\x08selector\x18\x02 \x01(\x0b\x32\x1d.jumpstarter.v1.LabelSelectorR\x08selector\"*\n\x14RequestLeaseResponse\x12\x12\n\x04name\x18\x01 \x01(\tR\x04name\")\n\x13ReleaseLeaseRequest\x12\x12\n\x04name\x18\x01 \x01(\tR\x04name\"\x16\n\x14ReleaseLeaseResponse\"\x13\n\x11ListLeasesRequest\"*\n\x12ListLeasesResponse\x12\x14\n\x05names\x18\x01 \x03(\tR\x05names2\xed\x07\n\x11\x43ontrollerService\x12M\n\x08Register\x12\x1f.jumpstarter.v1.RegisterRequest\x1a .jumpstarter.v1.RegisterResponse\x12S\n\nUnregister\x12!.jumpstarter.v1.UnregisterRequest\x1a\".jumpstarter.v1.UnregisterResponse\x12I\n\x06Listen\x12\x1d.jumpstarter.v1.ListenRequest\x1a\x1e.jumpstarter.v1.ListenResponse0\x01\x12I\n\x06Status\x12\x1d.jumpstarter.v1.StatusRequest\x1a\x1e.jumpstarter.v1.StatusResponse0\x01\x12\x41\n\x04\x44ial\x12\x1b.jumpstarter.v1.DialRequest\x1a\x1c.jumpstarter.v1.DialResponse\x12K\n\x0b\x41uditStream\x12\".jumpstarter.v1.AuditStreamRequest\x1a\x16.google.protobuf.Empty(\x01\x12\\\n\rListExporters\x12$.jumpstarter.v1.ListExportersRequest\x1a%.jumpstarter.v1.ListExportersResponse\x12V\n\x0bGetExporter\x12\".jumpstarter.v1.GetExporterRequest\x1a#.jumpstarter.v1.GetExporterResponse\x12M\n\x08GetLease\x12\x1f.jumpstarter.v1.GetLeaseRequest\x1a .jumpstarter.v1.GetLeaseResponse\x12Y\n\x0cRequestLease\x12#.jumpstarter.v1.RequestLeaseRequest\x1a$.jumpstarter.v1.RequestLeaseResponse\x12Y\n\x0cReleaseLease\x12#.jumpstarter.v1.ReleaseLeaseRequest\x1a$.jumpstarter.v1.ReleaseLeaseResponse\x12S\n\nListLeases\x12!.jumpstarter.v1.ListLeasesRequest\x1a\".jumpstarter.v1.ListLeasesResponse2\xb0\x03\n\x0f\x45xporterService\x12\x46\n\tGetReport\x12\x16.google.protobuf.Empty\x1a!.jumpstarter.v1.GetReportResponse\x12S\n\nDriverCall\x12!.jumpstarter.v1.DriverCallRequest\x1a\".jumpstarter.v1.DriverCallResponse\x12p\n\x13StreamingDriverCall\x12*.jumpstarter.v1.StreamingDriverCallRequest\x1a+.jumpstarter.v1.StreamingDriverCallResponse0\x01\x12H\n\tLogStream\x12\x16.google.protobuf.Empty\x1a!.jumpstarter.v1.LogStreamResponse0\x01\x12\x44\n\x05Reset\x12\x1c.jumpstarter.v1.ResetRequest\x1a\x1d.jumpstarter.v1.ResetResponseB\x7f\n\x12\x63om.jumpstarter.v1B\x10JumpstarterProtoP\x01\xa2\x02\x03JXX\xaa\x02\x0eJumpstarter.V1\xca\x02\x0eJumpstarter\\V1\xe2\x02\x1aJumpstarter\\V1\\GPBMetadata\xea\x02\x0fJumpstarter::V1b\x06proto3')

_globals = globals()
_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, _globals)
_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, 'jumpstarter.v1.jumpstarter_pb2', _globals)
if not _descriptor._USE_C_DESCRIPTORS:
  _globals['DESCRIPTOR']._loaded_options = None
  _globals['DESCRIPTOR']._serialized_options = b'\n\022com.jumpstarter.v1B\020JumpstarterProtoP\001\242\002\003JXX\252\002\016Jumpstarter.V1\312\002\016Jumpstarter\\V1\342\002\032Jumpstarter\\V1\\GPBMetadata\352\002\017Jumpstarter::V1'
  _globals['_REGISTERREQUEST_LABELSENTRY']._loaded_options = None
  _globals['_REGISTERREQUEST_LABELSENTRY']._serialized_options = b'8\001'
  _globals['_DRIVERINSTANCEREPORT_LABELSENTRY']._loaded_options = None
  _globals['_DRIVERINSTANCEREPORT_LABELSENTRY']._serialized_options = b'8\001'
  _globals['_GETREPORTRESPONSE_LABELSENTRY']._loaded_options = None
  _globals['_GETREPORTRESPONSE_LABELSENTRY']._serialized_options = b'8\001'
  _globals['_LISTEXPORTERSREQUEST_LABELSENTRY']._loaded_options = None
  _globals['_LISTEXPORTERSREQUEST_LABELSENTRY']._serialized_options = b'8\001'
  _globals['_REGISTERREQUEST']._serialized_start=210
  _globals['_REGISTERREQUEST']._serialized_end=419
  _globals['_REGISTERREQUEST_LABELSENTRY']._serialized_start=362
  _globals['_REGISTERREQUEST_LABELSENTRY']._serialized_end=419
  _globals['_DRIVERINSTANCEREPORT']._serialized_start=422
  _globals['_DRIVERINSTANCEREPORT']._serialized_end=651
  _globals['_DRIVERINSTANCEREPORT_LABELSENTRY']._serialized_start=362
  _globals['_DRIVERINSTANCEREPORT_LABELSENTRY']._serialized_end=419
  _globals['_REGISTERRESPONSE']._serialized_start=653
  _globals['_REGISTERRESPONSE']._serialized_end=691
  _globals['_UNREGISTERREQUEST']._serialized_start=693
  _globals['_UNREGISTERREQUEST']._serialized_end=736
  _globals['_UNREGISTERRESPONSE']._serialized_start=738
  _globals['_UNREGISTERRESPONSE']._serialized_end=758
  _globals['_LISTENREQUEST']._serialized_start=760
  _globals['_LISTENREQUEST']._serialized_end=806
  _globals['_LISTENRESPONSE']._serialized_start=808
  _globals['_LISTENRESPONSE']._serialized_end=900
  _globals['_STATUSREQUEST']._serialized_start=902
  _globals['_STATUSREQUEST']._serialized_end=917
  _globals['_STATUSRESPONSE']._serialized_start=920
  _globals['_STATUSRESPONSE']._serialized_end=1065
  _globals['_DIALREQUEST']._serialized_start=1067
  _globals['_DIALREQUEST']._serialized_end=1111
  _globals['_DIALRESPONSE']._serialized_start=1113
  _globals['_DIALRESPONSE']._serialized_end=1203
  _globals['_AUDITSTREAMREQUEST']._serialized_start=1206
  _globals['_AUDITSTREAMREQUEST']._serialized_end=1367
  _globals['_GETREPORTRESPONSE']._serialized_start=1370
  _globals['_GETREPORTRESPONSE']._serialized_end=1603
  _globals['_GETREPORTRESPONSE_LABELSENTRY']._serialized_start=362
  _globals['_GETREPORTRESPONSE_LABELSENTRY']._serialized_end=419
  _globals['_DRIVERCALLREQUEST']._serialized_start=1605
  _globals['_DRIVERCALLREQUEST']._serialized_end=1712
  _globals['_DRIVERCALLRESPONSE']._serialized_start=1714
  _globals['_DRIVERCALLRESPONSE']._serialized_end=1802
  _globals['_STREAMINGDRIVERCALLREQUEST']._serialized_start=1804
  _globals['_STREAMINGDRIVERCALLREQUEST']._serialized_end=1920
  _globals['_STREAMINGDRIVERCALLRESPONSE']._serialized_start=1922
  _globals['_STREAMINGDRIVERCALLRESPONSE']._serialized_end=2019
  _globals['_LOGSTREAMRESPONSE']._serialized_start=2021
  _globals['_LOGSTREAMRESPONSE']._serialized_end=2114
  _globals['_RESETREQUEST']._serialized_start=2116
  _globals['_RESETREQUEST']._serialized_end=2130
  _globals['_RESETRESPONSE']._serialized_start=2132
  _globals['_RESETRESPONSE']._serialized_end=2147
  _globals['_LISTEXPORTERSREQUEST']._serialized_start=2150
  _globals['_LISTEXPORTERSREQUEST']._serialized_end=2305
  _globals['_LISTEXPORTERSREQUEST_LABELSENTRY']._serialized_start=362
  _globals['_LISTEXPORTERSREQUEST_LABELSENTRY']._serialized_end=419
  _globals['_LISTEXPORTERSRESPONSE']._serialized_start=2307
  _globals['_LISTEXPORTERSRESPONSE']._serialized_end=2395
  _globals['_GETEXPORTERREQUEST']._serialized_start=2397
  _globals['_GETEXPORTERREQUEST']._serialized_end=2437
  _globals['_GETEXPORTERRESPONSE']._serialized_start=2439
  _globals['_GETEXPORTERRESPONSE']._serialized_end=2523
  _globals['_GETLEASEREQUEST']._serialized_start=2525
  _globals['_GETLEASEREQUEST']._serialized_end=2562
  _globals['_GETLEASERESPONSE']._serialized_start=2565
  _globals['_GETLEASERESPONSE']._serialized_end=2968
  _globals['_REQUESTLEASEREQUEST']._serialized_start=2971
  _globals['_REQUESTLEASEREQUEST']._serialized_end=3106
  _globals['_REQUESTLEASERESPONSE']._serialized_start=3108
  _globals['_REQUESTLEASERESPONSE']._serialized_end=3150
  _globals['_RELEASELEASEREQUEST']._serialized_start=3152
  _globals['_RELEASELEASEREQUEST']._serialized_end=3193
  _globals['_RELEASELEASERESPONSE']._serialized_start=3195
  _globals['_RELEASELEASERESPONSE']._serialized_end=3217
  _globals['_LISTLEASESREQUEST']._serialized_start=3219
  _globals['_LISTLEASESREQUEST']._serialized_end=3238
  _globals['_LISTLEASESRESPONSE']._serialized_start=3240
  _globals['_LISTLEASESRESPONSE']._serialized_end=3282
  _globals['_CONTROLLERSERVICE']._serialized_start=3285
  _globals['_CONTROLLERSERVICE']._serialized_end=4290
  _globals['_EXPORTERSERVICE']._serialized_start=4293
  _globals['_EXPORTERSERVICE']._serialized_end=4725
# @@protoc_insertion_point(module_scope)
