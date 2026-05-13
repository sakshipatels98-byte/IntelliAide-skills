package proposal

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"

	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/cli-runtime/pkg/genericclioptions"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
	sigsyaml "sigs.k8s.io/yaml"
)

const (
	OutputJSON = "json"
	OutputYAML = "yaml"
)

var scheme = func() *runtime.Scheme {
	s := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(s)
	_ = agenticv1alpha1.AddToScheme(s)
	return s
}()

func NewClient(f *genericclioptions.ConfigFlags) (client.Client, error) {
	cfg, err := f.ToRESTConfig()
	if err != nil {
		return nil, fmt.Errorf("failed to get REST config: %w", err)
	}
	return newClientFromConfig(cfg)
}

func newClientFromConfig(cfg *rest.Config) (client.Client, error) {
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		return nil, fmt.Errorf("failed to create client: %w", err)
	}
	return c, nil
}

func ResolveNamespace(f *genericclioptions.ConfigFlags) (string, error) {
	if f.Namespace != nil && *f.Namespace != "" {
		return *f.Namespace, nil
	}
	rawConfig, err := f.ToRawKubeConfigLoader().RawConfig()
	if err != nil {
		return "", fmt.Errorf("failed to load kubeconfig: %w", err)
	}
	if ctx, ok := rawConfig.Contexts[rawConfig.CurrentContext]; ok && ctx.Namespace != "" {
		return ctx.Namespace, nil
	}
	return "default", nil
}

func ValidateOutputFormat(format string) error {
	if format == "" {
		return nil
	}
	for _, v := range []string{OutputJSON, OutputYAML} {
		if format == v {
			return nil
		}
	}
	return fmt.Errorf("invalid output format %q, must be one of: json, yaml", format)
}

func MarshalOutput(w io.Writer, obj interface{}, format string) error {
	switch format {
	case OutputJSON:
		enc := json.NewEncoder(w)
		enc.SetIndent("", "  ")
		return enc.Encode(obj)
	case OutputYAML:
		data, err := sigsyaml.Marshal(obj)
		if err != nil {
			return err
		}
		_, err = w.Write(data)
		return err
	default:
		return fmt.Errorf("unknown output format: %s", format)
	}
}

func trimSpace(s string) string { return strings.TrimSpace(s) }
