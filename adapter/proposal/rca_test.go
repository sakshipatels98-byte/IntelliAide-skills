package proposal

import (
	"bytes"
	"context"
	"strings"
	"testing"

	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/cli-runtime/pkg/genericclioptions"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func testScheme() *runtime.Scheme {
	s := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(s)
	_ = agenticv1alpha1.AddToScheme(s)
	return s
}

func fakeStreams() (genericclioptions.IOStreams, *bytes.Buffer, *bytes.Buffer) {
	out, errOut := &bytes.Buffer{}, &bytes.Buffer{}
	return genericclioptions.IOStreams{In: strings.NewReader(""), Out: out, ErrOut: errOut}, out, errOut
}

func TestRCA_Success(t *testing.T) {
	streams, out, _ := fakeStreams()
	fc := fake.NewClientBuilder().WithScheme(testScheme()).Build()

	o := &RCAOptions{
		client:      fc,
		namespace:   "openshift-lightspeed",
		agent:       "smart",
		request:     "etcd pods not ready in openshift-etcd",
		skillsImage: defaultIntelliAideSkillsImage,
		IOStreams:    streams,
	}

	if err := o.Run(context.Background()); err != nil {
		t.Fatalf("Run: %v", err)
	}

	output := out.String()
	if !strings.Contains(output, "proposal/") {
		t.Errorf("expected proposal/ in output, got: %s", output)
	}
	if !strings.Contains(output, "created") {
		t.Errorf("expected 'created' in output, got: %s", output)
	}
}

func TestRCA_ProposalShape(t *testing.T) {
	streams, _, _ := fakeStreams()
	fc := fake.NewClientBuilder().WithScheme(testScheme()).Build()

	o := &RCAOptions{
		client:           fc,
		namespace:        "openshift-lightspeed",
		agent:            "smart",
		request:          "Cluster update stalled",
		skillsImage:      "quay.io/myorg/intelliaide-skills:dev",
		targetNamespaces: []string{"openshift-cluster-version"},
		IOStreams:         streams,
	}

	if err := o.Run(context.Background()); err != nil {
		t.Fatalf("Run: %v", err)
	}

	list := &agenticv1alpha1.ProposalList{}
	if err := fc.List(context.Background(), list); err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(list.Items) != 1 {
		t.Fatalf("expected 1 proposal, got %d", len(list.Items))
	}
	p := list.Items[0]

	if p.GenerateName != "rca-" {
		t.Errorf("expected GenerateName 'rca-', got %q", p.GenerateName)
	}
	if p.Labels["agentic.openshift.io/source"] != "intelliaide" {
		t.Errorf("expected source label 'intelliaide', got %q", p.Labels["agentic.openshift.io/source"])
	}
	if p.Spec.Analysis == nil || p.Spec.Analysis.Agent != "smart" {
		t.Errorf("expected analysis agent 'smart', got %v", p.Spec.Analysis)
	}
	if p.Spec.Execution != nil {
		t.Errorf("expected no execution step, got %v", p.Spec.Execution)
	}
	if len(p.Spec.Tools.Skills) != 1 {
		t.Fatalf("expected 1 skills source, got %d", len(p.Spec.Tools.Skills))
	}

	skill := p.Spec.Tools.Skills[0]
	if skill.Image != "quay.io/myorg/intelliaide-skills:dev" {
		t.Errorf("expected skills image, got %q", skill.Image)
	}
	// Paths must be set so only the skill wrapper is mounted, not the full engine.
	if len(skill.Paths) != 1 || skill.Paths[0] != intelliAideSkillsPath {
		t.Errorf("expected Paths=[%q], got %v", intelliAideSkillsPath, skill.Paths)
	}

	if p.Spec.Tools.OutputSchema == nil {
		t.Fatal("expected outputSchema to be set")
	}
	if _, ok := p.Spec.Tools.OutputSchema.Properties["rcaSummary"]; !ok {
		t.Error("expected outputSchema to have 'rcaSummary' property")
	}
}

func TestRCA_Validate(t *testing.T) {
	tests := []struct {
		name    string
		opts    RCAOptions
		wantErr bool
		errMsg  string
	}{
		{
			name:    "empty request",
			opts:    RCAOptions{request: "  ", skillsImage: defaultIntelliAideSkillsImage},
			wantErr: true,
			errMsg:  "--request",
		},
		{
			name:    "empty skills image",
			opts:    RCAOptions{request: "fix", skillsImage: ""},
			wantErr: true,
			errMsg:  "--skills-image",
		},
		{
			name:    "invalid output format",
			opts:    RCAOptions{request: "fix", skillsImage: defaultIntelliAideSkillsImage, output: "xml"},
			wantErr: true,
		},
		{
			name:    "valid minimal",
			opts:    RCAOptions{request: "fix", skillsImage: defaultIntelliAideSkillsImage},
			wantErr: false,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.opts.Validate()
			if (err != nil) != tc.wantErr {
				t.Errorf("Validate() error = %v, wantErr %v", err, tc.wantErr)
			}
			if tc.wantErr && tc.errMsg != "" && err != nil && !strings.Contains(err.Error(), tc.errMsg) {
				t.Errorf("error should contain %q, got: %v", tc.errMsg, err)
			}
		})
	}
}

func TestRCA_OutputSchemaStructure(t *testing.T) {
	schema, err := parseRCAOutputSchema()
	if err != nil {
		t.Fatalf("parseRCAOutputSchema: %v", err)
	}
	if schema.Type != "object" {
		t.Errorf("expected schema type 'object', got %q", schema.Type)
	}
	if _, ok := schema.Properties["rcaSummary"]; !ok {
		t.Fatal("outputSchema missing 'rcaSummary' property")
	}
	if _, ok := schema.Properties["options"]; !ok {
		t.Fatal("outputSchema missing 'options' property")
	}
}
