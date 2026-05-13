// oc-intelliaide is the CLI adapter that bridges OpenShift Lightspeed with the
// Agentic Operator for IntelliAide RCA workflows.
//
// When a user types a /rca keyword in OpenShift Lightspeed, Lightspeed calls:
//
//	oc-intelliaide proposal rca --request "<user text>"
//
// This creates a Proposal CR pre-configured with the IntelliAide skills image
// and output schema, then prints instructions for approving analysis.
package main

import (
	"os"

	"github.com/rh-ee/intelliaide-adapter/proposal"
	"github.com/spf13/cobra"
	"k8s.io/cli-runtime/pkg/genericclioptions"
)

func main() {
	streams := genericclioptions.IOStreams{In: os.Stdin, Out: os.Stdout, ErrOut: os.Stderr}

	root := &cobra.Command{
		Use:          "oc-intelliaide",
		Short:        "CLI adapter: OpenShift Lightspeed → IntelliAide RCA via Agentic Operator",
		SilenceUsage: true,
	}

	proposalCmd := &cobra.Command{
		Use:   "proposal",
		Short: "Manage IntelliAide RCA proposals",
	}
	proposalCmd.AddCommand(proposal.NewRCACmd(streams))

	root.AddCommand(proposalCmd)

	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}
