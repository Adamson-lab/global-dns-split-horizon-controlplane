package dns.splithorizon

import rego.v1

default allow := false
default reason := "denied: split-horizon view or record type not permitted"

allow if {
	input.horizon in {"internal", "external"}
	input.record_type in {"A", "AAAA", "CNAME", "TXT"}
	startswith(input.fqdn, "edge.")
}

reason := "allowed: valid split-horizon record mutation" if {
	allow
}
