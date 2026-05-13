package proposal

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	"github.com/spf13/cobra"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/cli-runtime/pkg/genericclioptions"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// defaultIntelliAideSkillsImage is the OCI image built from Dockerfile.skills
// in the IntelliAide repo. Override with --skills-image.
const defaultIntelliAideSkillsImage = "quay.io/rh-ee-cdate/intelliaide-skills:latest"

// intelliAideSkillsPath is the directory inside the skills image that contains
// CLAUDE.md and run_rca.py. Specifying the path here ensures only the
// IntelliAide skill wrapper is mounted — not the full engine at /app/.
const intelliAideSkillsPath = "/intelliaide"

// rcaOutputSchemaJSON is the JSON Schema sent to Claude to enforce structured
// output that matches IntelliAide's rca_structured response format.
//
// Top-level fields:
//   - options[]    — standard RemediationOption array (one per root cause)
//   - rcaSummary   — verbatim IntelliAide rca_structured sections
//
// Each rcaSummary field maps 1:1 to a key in rca_structured returned by
// IntelliAide's get_job_result MCP tool.
const rcaOutputSchemaJSON = `{
  "type": "object",
  "required": ["options", "rcaSummary"],
  "properties": {
    "options": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["title", "diagnosis", "proposal"],
        "properties": {
          "title":   { "type": "string" },
          "summary": { "type": "string" },
          "diagnosis": {
            "type": "object",
            "required": ["summary", "confidence", "rootCause"],
            "properties": {
              "summary":    { "type": "string",
                "description": "Maps to rca_structured.executive_summary + chronology_of_events" },
              "confidence": { "type": "string", "enum": ["Low", "Medium", "High"] },
              "rootCause":  { "type": "string",
                "description": "One root cause from rca_structured.primary_root_causes" }
            }
          },
          "proposal": {
            "type": "object",
            "required": ["description", "actions", "risk", "reversible"],
            "properties": {
              "description": { "type": "string",
                "description": "Corresponding entry from rca_structured.recommendations" },
              "actions": {
                "type": "array",
                "items": {
                  "type": "object",
                  "required": ["type", "description"],
                  "properties": {
                    "type":        { "type": "string" },
                    "description": { "type": "string" }
                  }
                }
              },
              "risk":       { "type": "string", "enum": ["Low", "Medium", "High", "Critical"] },
              "reversible": { "type": "string", "enum": ["Reversible", "Irreversible", "Partial"] }
            }
          },
          "verification": { "type": "object" },
          "rbac":         { "type": "object" }
        }
      }
    },
    "rcaSummary": {
      "type": "object",
      "description": "Verbatim IntelliAide rca_structured output. Each field maps directly to an rca_structured key from get_job_result.",
      "required": ["executiveSummary", "primaryRootCauses", "recommendations"],
      "properties": {
        "executiveSummary":         { "type": "string",
          "description": "rca_structured.executive_summary" },
        "chronologyOfEvents":       { "type": "string",
          "description": "rca_structured.chronology_of_events" },
        "primaryRootCauses":        { "type": "string",
          "description": "rca_structured.primary_root_causes (Markdown)" },
        "secondaryCauses":          { "type": "string",
          "description": "rca_structured.secondary_causes" },
        "aggregatedErrorPatterns":  { "type": "string",
          "description": "rca_structured.aggregated_error_patterns" },
        "recommendations":          { "type": "string",
          "description": "rca_structured.recommendations (Markdown)" },
        "evidenceFiles": {
          "type": "array",
          "items": { "type": "string" },
          "description": "Cluster files analysed as evidence"
        },
        "totalCostUsd": { "type": "number",
          "description": "Total LLM cost in USD for this RCA run" }
      }
    }
  }
}`

// RCAOptions holds flags and resolved state for the rca subcommand.
type RCAOptions struct {
	configFlags      *genericclioptions.ConfigFlags
	request          string
	agent            string
	skillsImage      string
	targetNamespaces []string
	output           string

	client    client.Client
	namespace string

	genericclioptions.IOStreams
}

// NewRCACmd returns the `oc-intelliaide proposal rca` command.
//
// Usage:
//
//	# Triggered by OpenShift Lightspeed when a user types a /rca keyword:
//	oc-intelliaide proposal rca --request "etcd pods not ready in openshift-etcd"
//
//	# Explicit namespace and agent:
//	oc-intelliaide proposal rca \
//	  --request "Cluster update stalled — CVO reports Degraded" \
//	  --namespace openshift-lightspeed \
//	  --agent smart
//
//	# Override skills image (e.g. during development):
//	oc-intelliaide proposal rca \
//	  --request "OOMKilled pods in production" \
//	  --skills-image quay.io/myorg/intelliaide-skills:dev
func NewRCACmd(streams genericclioptions.IOStreams) *cobra.Command {
	o := &RCAOptions{
		configFlags: genericclioptions.NewConfigFlags(true),
		agent:       "smart",
		skillsImage: defaultIntelliAideSkillsImage,
		IOStreams:    streams,
	}

	cmd := &cobra.Command{
		Use:   "rca",
		Short: "Run IntelliAide root cause analysis for an issue reported in OpenShift Lightspeed",
		Long: `Creates a Proposal CR pre-configured with the IntelliAide AI skills image.

The Proposal's analysis step mounts only the IntelliAide skill wrapper
(` + intelliAideSkillsPath + `/) into the Claude sandbox and instructs Claude to:
  1. Call run_rca.py with the user's problem statement.
  2. Poll until the RCA job completes (10-30 min).
  3. Map the structured RCA report into RemediationOptions stored in
     an AnalysisResult CR.

This command is the integration point between OpenShift Lightspeed (where
users report issues) and IntelliAide (which performs the live-cluster RCA).
When a user types a /rca keyword in Lightspeed, Lightspeed calls this command
with the user's text as --request.`,
		Example: `  # Triggered by Lightspeed keyword (/rca)
  oc-intelliaide proposal rca --request "etcd pods not ready in openshift-etcd"

  # Full pipeline with explicit namespace and target namespaces
  oc-intelliaide proposal rca \
    --request "Cluster update stalled — CVO Degraded" \
    --namespace openshift-lightspeed \
    --target-namespaces openshift-cluster-version,openshift-machine-config-operator

  # Development override of skills image
  oc-intelliaide proposal rca \
    --request "OOMKilled pods in production" \
    --skills-image quay.io/myorg/intelliaide-skills:dev`,
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := o.Complete(cmd, args); err != nil {
				return err
			}
			if err := o.Validate(); err != nil {
				return err
			}
			return o.Run(cmd.Context())
		},
	}

	o.configFlags.AddFlags(cmd.Flags())
	cmd.Flags().StringVar(&o.request, "request", "", "User's problem statement (required) — the text from OpenShift Lightspeed")
	cmd.Flags().StringVar(&o.agent, "agent", "smart", "Analysis agent CR name (smart model recommended for RCA)")
	cmd.Flags().StringVar(&o.skillsImage, "skills-image", defaultIntelliAideSkillsImage, "IntelliAide skills OCI image reference")
	cmd.Flags().StringSliceVar(&o.targetNamespaces, "target-namespaces", nil, "Kubernetes namespaces to focus the RCA on (comma-separated)")
	cmd.Flags().StringVarP(&o.output, "output", "o", "", "Output format: json or yaml")

	_ = cmd.MarkFlagRequired("request")

	return cmd
}

func (o *RCAOptions) Complete(_ *cobra.Command, _ []string) error {
	var err error
	o.client, err = NewClient(o.configFlags)
	if err != nil {
		return err
	}
	o.namespace, err = ResolveNamespace(o.configFlags)
	return err
}

func (o *RCAOptions) Validate() error {
	if strings.TrimSpace(o.request) == "" {
		return fmt.Errorf("--request must not be empty")
	}
	if strings.TrimSpace(o.skillsImage) == "" {
		return fmt.Errorf("--skills-image must not be empty")
	}
	return ValidateOutputFormat(o.output)
}

func (o *RCAOptions) Run(ctx context.Context) error {
	outputSchema, err := parseRCAOutputSchema()
	if err != nil {
		return fmt.Errorf("internal error: parse RCA output schema: %w", err)
	}

	proposal := &agenticv1alpha1.Proposal{
		ObjectMeta: metav1.ObjectMeta{
			GenerateName: "rca-",
			Namespace:    o.namespace,
			Labels: map[string]string{
				"agentic.openshift.io/source": "intelliaide",
			},
		},
		Spec: agenticv1alpha1.ProposalSpec{
			Request:          strings.TrimSpace(o.request),
			TargetNamespaces: o.targetNamespaces,
			Analysis: &agenticv1alpha1.ProposalStep{
				Agent: o.agent,
			},
			// Advisory only — IntelliAide RCA produces findings; humans
			// decide on follow-up actions. No execution or verification step.
			Tools: agenticv1alpha1.ToolsSpec{
				Skills: []agenticv1alpha1.SkillsSource{
					{
						Image: o.skillsImage,
						// Mount only the skill wrapper directory, not the full
						// IntelliAide engine at /app/. This keeps the sandbox
						// volume small and prevents accidental engine exposure.
						Paths: []string{intelliAideSkillsPath},
					},
				},
				OutputSchema: outputSchema,
			},
		},
	}

	if err := o.client.Create(ctx, proposal); err != nil {
		return fmt.Errorf("failed to create RCA proposal: %w", err)
	}

	if o.output == OutputJSON || o.output == OutputYAML {
		return MarshalOutput(o.Out, proposal, o.output)
	}

	fmt.Fprintf(o.Out, "proposal/%s created\n", proposal.Name)
	fmt.Fprintf(o.Out, "Approve analysis to start RCA: oc patch proposalapproval %s -n %s --type merge -p '{\"spec\":{\"stages\":[{\"type\":\"Analysis\",\"decision\":\"Approved\",\"analysis\":{}}]}}'\n",
		proposal.Name, proposal.Namespace)
	return nil
}

// parseRCAOutputSchema unmarshals the embedded JSON schema constant into a
// JSONSchemaProps value that can be stored on the Proposal spec.
func parseRCAOutputSchema() (*apiextensionsv1.JSONSchemaProps, error) {
	var schema apiextensionsv1.JSONSchemaProps
	if err := json.Unmarshal([]byte(rcaOutputSchemaJSON), &schema); err != nil {
		return nil, err
	}
	return &schema, nil
}
