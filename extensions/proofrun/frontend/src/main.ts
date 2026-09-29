// Native Federation entry point. This remote is never bootstrapped standalone — the host lazy-loads
// the exposed './Extension' module by remoteEntry URL. initFederation() only initialises the shared
// scope so the remote's build graph is federation-aware.
import { initFederation } from '@angular-architects/native-federation';

initFederation().catch(err => console.error(err));
