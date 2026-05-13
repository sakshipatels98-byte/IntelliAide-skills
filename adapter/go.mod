module github.com/rh-ee/intelliaide-adapter

go 1.22

require (
	github.com/openshift/lightspeed-agentic-operator/api v0.0.0
	github.com/spf13/cobra v1.8.0
	k8s.io/apiextensions-apiserver v0.35.3
	k8s.io/apimachinery v0.35.3
	k8s.io/cli-runtime v0.35.3
	k8s.io/client-go v0.35.3
	sigs.k8s.io/controller-runtime v0.23.3
	sigs.k8s.io/yaml v1.4.0
)

// Use the local API module for development; replace with a pinned version once
// lightspeed-agentic-operator/api is published to a registry.
replace github.com/openshift/lightspeed-agentic-operator/api => ../../intel-lightspeed-agentic-operator/api
