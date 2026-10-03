package service

import (
	"context"
	"net"
	"os"
	"reflect"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"
	pb "github.com/jumpstarter-dev/jumpstarter/controller/internal/protocol/jumpstarter/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protodesc"
	"google.golang.org/protobuf/reflect/protoregistry"
	"google.golang.org/protobuf/types/dynamicpb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type reportStatusFixture struct {
	service *ControllerService
	client  client.WithWatch
	key     client.ObjectKey
}

func newReportStatusFixture(t *testing.T, c client.WithWatch, namespace string) reportStatusFixture {
	t.Helper()
	if c == nil {
		scheme := runtime.NewScheme()
		if err := jumpstarterdevv1alpha1.AddToScheme(scheme); err != nil {
			t.Fatal(err)
		}
		c = fake.NewClientBuilder().WithScheme(scheme).
			WithStatusSubresource(&jumpstarterdevv1alpha1.Exporter{}, &jumpstarterdevv1alpha1.Lease{}).Build()
	}
	exporter := &jumpstarterdevv1alpha1.Exporter{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: "status-exporter",
	}}
	if err := c.Create(context.Background(), exporter); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := c.Delete(context.Background(), exporter); client.IgnoreNotFound(err) != nil {
			t.Error(err)
		}
	})
	return reportStatusFixture{
		service: &ControllerService{
			Client: c, Authn: &passingAuthenticator{userName: "status-test"}, Authz: passingAuthorizer{},
			Attr: &exporterAttributesGetter{namespace: namespace, name: exporter.Name},
		},
		client: c, key: client.ObjectKeyFromObject(exporter),
	}
}

func (f reportStatusFixture) exporter(t *testing.T) *jumpstarterdevv1alpha1.Exporter {
	t.Helper()
	exporter := &jumpstarterdevv1alpha1.Exporter{}
	if err := f.client.Get(context.Background(), f.key, exporter); err != nil {
		t.Fatal(err)
	}
	return exporter
}

func (f reportStatusFixture) assign(t *testing.T, lease string) {
	t.Helper()
	exporter := f.exporter(t)
	exporter.Status.LeaseRef = nil
	if lease != "" {
		exporter.Status.LeaseRef = &corev1.LocalObjectReference{Name: lease}
	}
	if err := f.client.Status().Update(context.Background(), exporter); err != nil {
		t.Fatal(err)
	}
}

func (f reportStatusFixture) lease(t *testing.T, name string) *jumpstarterdevv1alpha1.Lease {
	t.Helper()
	lease := &jumpstarterdevv1alpha1.Lease{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: f.key.Namespace},
		Spec: jumpstarterdevv1alpha1.LeaseSpec{
			ClientRef:   corev1.LocalObjectReference{Name: "test-client"},
			ExporterRef: &corev1.LocalObjectReference{Name: f.key.Name},
			Duration:    &metav1.Duration{Duration: time.Hour},
		},
	}
	if err := f.client.Create(context.Background(), lease); err != nil {
		t.Fatal(err)
	}
	lease.Status.ExporterRef = &corev1.LocalObjectReference{Name: f.key.Name}
	if err := f.client.Status().Update(context.Background(), lease); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := f.client.Delete(context.Background(), lease); client.IgnoreNotFound(err) != nil {
			t.Error(err)
		}
	})
	return lease
}

func (f reportStatusFixture) assertReleased(t *testing.T, lease *jumpstarterdevv1alpha1.Lease, want bool) {
	t.Helper()
	if err := f.client.Get(context.Background(), client.ObjectKeyFromObject(lease), lease); err != nil {
		t.Fatal(err)
	}
	if lease.Spec.Release != want {
		t.Fatalf("lease %s release=%t, want %t", lease.Name, lease.Spec.Release, want)
	}
}

func TestReportStatusLeasePolicy(t *testing.T) {
	statuses := []pb.ExporterStatus{
		pb.ExporterStatus_EXPORTER_STATUS_AVAILABLE, pb.ExporterStatus_EXPORTER_STATUS_OFFLINE,
		pb.ExporterStatus_EXPORTER_STATUS_BEFORE_LEASE_HOOK, pb.ExporterStatus_EXPORTER_STATUS_LEASE_READY,
		pb.ExporterStatus_EXPORTER_STATUS_BEFORE_LEASE_HOOK_FAILED,
		pb.ExporterStatus_EXPORTER_STATUS_AFTER_LEASE_HOOK, pb.ExporterStatus_EXPORTER_STATUS_AFTER_LEASE_HOOK_FAILED,
	}
	idle := []pb.ExporterStatus{pb.ExporterStatus_EXPORTER_STATUS_AVAILABLE, pb.ExporterStatus_EXPORTER_STATUS_OFFLINE}
	cleanup := append(slices.Clone(idle), pb.ExporterStatus_EXPORTER_STATUS_AFTER_LEASE_HOOK,
		pb.ExporterStatus_EXPORTER_STATUS_AFTER_LEASE_HOOK_FAILED)
	for _, tc := range []struct {
		name     string
		assigned string
		identity *string
		allowed  []pb.ExporterStatus
	}{
		{"legacy-idle", "", nil, statuses},
		{"legacy-assigned", "lease-b", nil, statuses},
		{"startup-shutdown", "", proto.String(""), idle},
		{"unleased-cannot-overwrite-assignment", "lease-b", proto.String(""), nil},
		{"current-lease", "lease-a", proto.String("lease-a"), statuses},
		{"ended-lease-cleanup", "", proto.String("lease-a"), cleanup},
		{"obsolete-lease", "lease-b", proto.String("lease-a"), nil},
	} {
		for _, value := range statuses {
			for _, release := range []bool{false, true} {
				name := tc.name + "/" + value.String()
				if release {
					name += "/release"
				}
				t.Run(name, func(t *testing.T) {
					f := newReportStatusFixture(t, nil, "default")
					f.assign(t, tc.assigned)
					before := f.exporter(t)
					_, err := f.service.ReportStatus(context.Background(), &pb.ReportStatusRequest{
						Status: value, LeaseName: tc.identity, Message: proto.String("report"), ReleaseLease: proto.Bool(release),
					})
					allowed := slices.Contains(tc.allowed, value)
					if tc.name == "startup-shutdown" && release {
						allowed = false
					}
					after := f.exporter(t)
					if allowed {
						if err != nil || after.Status.ExporterStatusValue != protoStatusToString(value) ||
							after.Status.StatusMessage != "report" || after.Status.LastSeen.IsZero() {
							t.Fatalf("allowed report: err=%v, status=%+v", err, after.Status)
						}
					} else if status.Code(err) != codes.FailedPrecondition || !reflect.DeepEqual(before, after) {
						t.Fatalf("rejected report changed exporter or returned wrong error: %v\nbefore=%+v\nafter=%+v", err, before, after)
					}
				})
			}
		}
	}
}

// Delay only the selected read/write. The underlying client still enforces
// resource versions, so reassignment must produce a real patch conflict.
type delayedReportClient struct {
	client.WithWatch
	beforeRead  func()
	beforePatch func(client.Object)
	afterPatch  func(client.Object)
	conflicts   atomic.Int32
}

func (c *delayedReportClient) Get(ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
	if c.beforeRead != nil {
		c.beforeRead()
	}
	return c.WithWatch.Get(ctx, key, obj, opts...)
}

func (c *delayedReportClient) Status() client.SubResourceWriter {
	return &delayedReportWriter{SubResourceWriter: c.WithWatch.Status(), client: c}
}

type delayedReportWriter struct {
	client.SubResourceWriter
	client *delayedReportClient
}

func (w *delayedReportWriter) Patch(ctx context.Context, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
	if w.client.beforePatch != nil {
		w.client.beforePatch(obj)
	}
	err := w.SubResourceWriter.Patch(ctx, obj, patch, opts...)
	if apierrors.IsConflict(err) {
		w.client.conflicts.Add(1)
	}
	if err == nil && w.client.afterPatch != nil {
		w.client.afterPatch(obj)
	}
	return err
}

func awaitReportSignal[T any](t *testing.T, ch <-chan T) T {
	t.Helper()
	select {
	case result := <-ch:
		return result
	case <-time.After(10 * time.Second):
		t.Fatal("timed out waiting for report test barrier")
		var zero T
		return zero
	}
}

func testReportStatusLateHandler(t *testing.T, c client.WithWatch, namespace string) {
	for _, value := range []pb.ExporterStatus{pb.ExporterStatus_EXPORTER_STATUS_AVAILABLE, pb.ExporterStatus_EXPORTER_STATUS_LEASE_READY} {
		for _, stage := range []string{"before-read", "before-write", "cleanup-before-write"} {
			if stage == "cleanup-before-write" && value == pb.ExporterStatus_EXPORTER_STATUS_LEASE_READY {
				continue // Rejected before any write by TestReportStatusLeasePolicy.
			}
			t.Run(value.String()+"/"+stage, func(t *testing.T) {
				f := newReportStatusFixture(t, c, namespace)
				leaseB := f.lease(t, "lease-b")
				f.assign(t, "lease-a")
				if stage == "cleanup-before-write" {
					f.assign(t, "")
				}
				entered, resume := make(chan struct{}), make(chan struct{})
				var resumed atomic.Bool
				unblock := func() {
					if resumed.CompareAndSwap(false, true) {
						close(resume)
					}
				}
				t.Cleanup(unblock)
				block := func() { close(entered); <-resume }
				delayed := &delayedReportClient{WithWatch: f.client}
				if stage == "before-read" {
					var reads atomic.Int32
					delayed.beforeRead = func() {
						// Authentication reads first; hold the status-write read.
						if reads.Add(1) == 2 {
							block()
						}
					}
				} else {
					var blocked atomic.Bool
					delayed.beforePatch = func(obj client.Object) {
						if obj.(*jumpstarterdevv1alpha1.Exporter).Status.StatusMessage == "late-a" && blocked.CompareAndSwap(false, true) {
							block()
						}
					}
				}
				f.service.Client = delayed
				finished := make(chan error, 1)
				server := grpc.NewServer(grpc.UnaryInterceptor(func(ctx context.Context, req any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
					// Model an accepted server operation outliving its client's deadline.
					// The fence must work independently of transport cancellation.
					response, err := handler(context.WithoutCancel(ctx), req)
					if req.(*pb.ReportStatusRequest).GetMessage() == "late-a" {
						finished <- err
					}
					return response, err
				}))
				pb.RegisterControllerServiceServer(server, f.service)
				listener, err := net.Listen("tcp", "127.0.0.1:0")
				if err != nil {
					t.Fatal(err)
				}
				go func() { _ = server.Serve(listener) }()
				t.Cleanup(server.Stop)
				conn, err := grpc.NewClient("passthrough:///"+listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
				if err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = conn.Close() })
				rpc := pb.NewControllerServiceClient(conn)
				deadline, cancel := context.WithTimeout(context.Background(), time.Second)
				defer cancel()
				result := make(chan error, 1)
				go func() {
					_, err := rpc.ReportStatus(deadline, &pb.ReportStatusRequest{
						Status: value, LeaseName: proto.String("lease-a"), Message: proto.String("late-a"),
					})
					result <- err
				}()
				awaitReportSignal(t, entered)
				if err := awaitReportSignal(t, result); status.Code(err) != codes.DeadlineExceeded {
					t.Fatalf("client deadline: %v", err)
				}
				t.Log("A client deadline exceeded; A server handler is still held")
				f.assign(t, "lease-b")
				currentStatus := pb.ExporterStatus_EXPORTER_STATUS_LEASE_READY
				if value == pb.ExporterStatus_EXPORTER_STATUS_LEASE_READY {
					currentStatus = pb.ExporterStatus_EXPORTER_STATUS_BEFORE_LEASE_HOOK
				}
				currentCtx, currentCancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer currentCancel()
				_, err = rpc.ReportStatus(currentCtx, &pb.ReportStatusRequest{
					Status: currentStatus, LeaseName: proto.String("lease-b"), Message: proto.String("current-b"),
				})
				if err != nil {
					t.Fatal(err)
				}
				before := f.exporter(t)
				t.Logf("B accepted: lease=%s status=%s message=%s rv=%s", before.Status.LeaseRef.Name,
					before.Status.ExporterStatusValue, before.Status.StatusMessage, before.ResourceVersion)
				unblock()
				completionErr := awaitReportSignal(t, finished)
				after := f.exporter(t)
				t.Logf("A completed: code=%s; stored lease=%s status=%s message=%s rv=%s conflicts=%d", status.Code(completionErr),
					after.Status.LeaseRef.Name, after.Status.ExporterStatusValue, after.Status.StatusMessage, after.ResourceVersion,
					delayed.conflicts.Load())
				if status.Code(completionErr) != codes.FailedPrecondition {
					t.Errorf("late server completion: %v", completionErr)
				}
				if !reflect.DeepEqual(before, after) {
					t.Fatalf("late A report changed B: before=%+v after=%+v", before.Status, after.Status)
				}
				f.assertReleased(t, leaseB, false)
				wantConflicts := int32(1)
				if stage == "before-read" {
					wantConflicts = 0
				}
				if got := delayed.conflicts.Load(); got != wantConflicts {
					t.Fatalf("conflicts=%d, want %d", got, wantConflicts)
				}
			})
		}
	}
}

func TestReportStatusLateHandler(t *testing.T) {
	testReportStatusLateHandler(t, nil, "default")
}

func TestReportStatusCleanupConflict(t *testing.T) {
	for _, clearRef := range []bool{false, true} {
		t.Run(map[bool]string{false: "same-lease", true: "cleared-ref"}[clearRef], func(t *testing.T) {
			f := newReportStatusFixture(t, nil, "default")
			f.assign(t, "lease-a")
			var once atomic.Bool
			delayed := &delayedReportClient{WithWatch: f.client, beforePatch: func(client.Object) {
				if once.CompareAndSwap(false, true) {
					if clearRef {
						f.assign(t, "")
					} else {
						exporter := f.exporter(t)
						exporter.Status.StatusMessage = "concurrent heartbeat"
						if err := f.client.Status().Update(context.Background(), exporter); err != nil {
							t.Fatal(err)
						}
					}
				}
			}}
			f.service.Client = delayed
			_, err := f.service.ReportStatus(context.Background(), &pb.ReportStatusRequest{
				Status: pb.ExporterStatus_EXPORTER_STATUS_AVAILABLE, LeaseName: proto.String("lease-a"),
			})
			if err != nil || delayed.conflicts.Load() != 1 ||
				f.exporter(t).Status.ExporterStatusValue != jumpstarterdevv1alpha1.ExporterStatusAvailable {
				t.Fatalf("cleanup did not survive conflict: %v (conflicts=%d)", err, delayed.conflicts.Load())
			}
		})
	}
}

func TestReportStatusReleaseUsesAcceptedIdentity(t *testing.T) {
	f := newReportStatusFixture(t, nil, "default")
	leaseA, leaseB := f.lease(t, "lease-a"), f.lease(t, "lease-b")
	f.assign(t, "lease-a")
	f.service.Client = &delayedReportClient{WithWatch: f.client, afterPatch: func(client.Object) {
		f.assign(t, "lease-b")
	}}
	_, err := f.service.ReportStatus(context.Background(), &pb.ReportStatusRequest{
		Status: pb.ExporterStatus_EXPORTER_STATUS_AFTER_LEASE_HOOK, LeaseName: proto.String("lease-a"),
		ReleaseLease: proto.Bool(true),
	})
	if err != nil {
		t.Fatal(err)
	}
	f.assertReleased(t, leaseA, true)
	f.assertReleased(t, leaseB, false)
}

func TestReportStatusOldControllerWireCompatibility(t *testing.T) {
	// Reconstruct the previous schema, then decode a new exporter request with
	// it. An old controller ignores lease_name and retains its old behavior.
	file := protodesc.ToFileDescriptorProto(pb.File_jumpstarter_v1_jumpstarter_proto)
	for _, message := range file.MessageType {
		if message.GetName() == "ReportStatusRequest" {
			message.Field = message.Field[:3]
			message.OneofDecl = message.OneofDecl[:2]
		}
	}
	oldFile, err := protodesc.NewFile(file, protoregistry.GlobalFiles)
	if err != nil {
		t.Fatal(err)
	}
	oldRequest := dynamicpb.NewMessage(oldFile.Messages().ByName("ReportStatusRequest"))
	for _, identity := range []string{"", "lease-a"} {
		request := &pb.ReportStatusRequest{
			Status: pb.ExporterStatus_EXPORTER_STATUS_AVAILABLE, Message: proto.String("cleanup"),
			ReleaseLease: proto.Bool(true), LeaseName: proto.String(identity),
		}
		wire, err := proto.Marshal(request)
		if err != nil {
			t.Fatal(err)
		}
		if err := (proto.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(wire, oldRequest); err != nil {
			t.Fatal(err)
		}
		legacyWire, err := proto.Marshal(oldRequest)
		if err != nil {
			t.Fatal(err)
		}
		legacy := &pb.ReportStatusRequest{}
		if err := proto.Unmarshal(legacyWire, legacy); err != nil {
			t.Fatal(err)
		}
		request.LeaseName = nil
		if !proto.Equal(request, legacy) {
			t.Fatalf("old controller lost known fields: %v", legacy)
		}
	}
}

func TestReportStatusLateHandlerKind(t *testing.T) {
	path := os.Getenv("REPORT_STATUS_KIND_KUBECONFIG")
	if path == "" {
		t.Skip("set REPORT_STATUS_KIND_KUBECONFIG to the dedicated local Kind kubeconfig")
	}
	config, err := clientcmd.LoadFromFile(path)
	if err != nil {
		t.Fatal(err)
	}
	const kindContext = "kind-jumpstarter-lease-verification"
	if config.CurrentContext != kindContext {
		t.Fatalf("refusing non-test context %q", config.CurrentContext)
	}
	restConfig, err := clientcmd.NewNonInteractiveClientConfig(*config, kindContext, &clientcmd.ConfigOverrides{}, nil).ClientConfig()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(restConfig.Host, "https://127.0.0.1:") {
		t.Fatalf("refusing non-loopback API server %q", restConfig.Host)
	}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, jumpstarterdevv1alpha1.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	c, err := client.NewWithWatch(restConfig, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	namespace := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "report-status-fencing-"}}
	if err := c.Create(context.Background(), namespace); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := c.Delete(context.Background(), namespace); err != nil {
			t.Error(err)
		}
	})
	t.Logf("testing real API status conflicts in %s", namespace.Name)
	testReportStatusLateHandler(t, c, namespace.Name)
}

// Invoked only by tools/reproducers/report_status_kind.py inside its isolated
// Kind Job. The Job's service account has access only to this namespace.
func TestReportStatusLateHandlerInCluster(t *testing.T) {
	namespace := os.Getenv("REPORT_STATUS_KIND_NAMESPACE")
	if namespace == "" {
		t.Skip("run tools/reproducers/report_status_kind.py for the in-cluster reproducer")
	}
	if !strings.HasPrefix(namespace, "report-status-repro-") {
		t.Fatalf("refusing non-reproducer namespace %q", namespace)
	}
	restConfig, err := rest.InClusterConfig()
	if err != nil {
		t.Fatal(err)
	}
	scheme := runtime.NewScheme()
	if err := jumpstarterdevv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	c, err := client.NewWithWatch(restConfig, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("in-cluster reproducer namespace: %s", namespace)
	testReportStatusLateHandler(t, c, namespace)
}
