package dns.failover

import rego.v1

default allow := false
default reason := "denied: failover outside UTC change window without active incident grace lock"

# Allow off-window health-check failover up to 15 minutes during active incident mitigation.
incident_grace if {
	input.grace_lock.active == true
	count(input.grace_lock.incident_id) >= 4
	input.grace_lock.extra_minutes <= 15
	input.actor_role in {"dns-sre", "incident-cmd"}
}

allow if {
	incident_grace
}

allow if {
	not incident_grace
	input.actor_role in {"dns-sre", "incident-cmd", "dns-reconciler"}
	input.utc_hour >= 1
	input.utc_hour <= 23
}

reason := "allowed via active incident grace lock" if {
	allow
	incident_grace
}

reason := "allowed within UTC DNS change window" if {
	allow
	not incident_grace
}
