const { withNativeFederation, share, NG_SKIP_LIST } =
  require('@angular-architects/native-federation/config');

// Native Federation REMOTE. Exposes a Routes array ('./Extension') that the host lazy-loads at
// runtime by remoteEntry URL (see duplo-ui/portal/ai-studio/src/app/extension-route-registrar.ts).
//
// IMPORTANT: REMOTE_NAME must be UNIQUE across all installed extensions — it is the federation
// container name and must equal manifest.frontend.remote.remoteName. Rename it per extension.
//
// The `shared` list must be a SUBSET of duplo-ui/portal/federation.shared.js, using the same options
// per entry. Anything with forRoot() providers, DI tokens or module-level state has to resolve to ONE
// instance across host and remote; a package the remote bundles privately gets a different class
// identity, so the host's forRoot provider no longer satisfies it (NullInjectorError / NG0201).
//
// Subset, NOT a mirror, for two reasons:
//   - `requiredVersion: 'auto'` makes share() call lookupVersion(), which THROWS for any package not
//     declared in THIS package.json. The host shares utilities an extension does not depend on
//     (e.g. 'yaml'); copying them here fails the build at config load.
//   - scripts/verify-remote-federation.js enforces remote ⊆ host, so a subset passes and a
//     remote-only package fails.
//
// Each entry gets its OWN object literal. Do NOT hoist a shared `const S = {...}` and reuse it:
// share() shallow-copies the map and then MUTATES each value in place (sets requiredVersion/version,
// deletes includeSecondaries). One reused object means the first key stamps its version onto the
// object, every later key sees requiredVersion !== 'auto', skips its own lookup, and ships the wrong
// version metadata. The host's federation.shared.js writes a fresh literal per package for this reason.
//
// The platform UI library @duplocloud-internal/ng-common-lib itself is deliberately NOT shared: the
// host compiles its own copy under a different specifier, so it is bundled into this remote.

// The ONE line to change per extension.
const REMOTE_NAME = 'duploExtensionProofRun';

module.exports = withNativeFederation({
  name: REMOTE_NAME,

  exposes: {
    './Extension': './src/app/extension.routes.ts',
  },

  shared: {
    ...share({
      // Angular + RxJS framework — must be a single instance across host + remotes.
      '@angular/core':              { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      '@angular/common':            { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      '@angular/forms':             { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      '@angular/router':            { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      '@angular/platform-browser':  { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      '@angular/cdk':               { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      '@angular/material':          { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      'rxjs':                       { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      // UI library peers that provide DI services / module-scoped providers.
      '@ng-bootstrap/ng-bootstrap': { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      '@ngx-translate/core':        { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      'ngx-toastr':                 { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      '@ng-select/ng-select':       { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      '@swimlane/ngx-datatable':    { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      'ngx-markdown':               { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      'ngx-monaco-editor-v2':       { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      // ng-common-lib peers carrying DI services / forRoot config. CoreCommonModule imports plain
      // FlexLayoutModule, whose SERVER_TOKEN only .withConfig() provides — the host does that, so a
      // privately bundled copy here fails with NG0201 FlexLayoutServerLoaded.
      '@ngbracket/ngx-layout':      { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      'ngx-echarts':                { singleton: true, strictVersion: false, requiredVersion: 'auto' },
      'angularx-flatpickr':         { singleton: true, strictVersion: false, requiredVersion: 'auto' },
    }),
  },

  // NOTE: `skip` REPLACES the built-in list rather than extending it, so NG_SKIP_LIST must be spread
  // in explicitly — dropping it would un-skip es-module-shims, zone.js and @softarc/native-federation*.
  // The /schematics/ rule excludes @angular/cdk's Node-only "./schematics" entry point, which cannot
  // survive browser bundling. @ngbracket/ngx-layout/server is the same class of entry: NF's
  // secondary-entry-point expansion picks it up and it imports @angular/platform-server, which a
  // browser-only app does not install. Both mirror the host's federation.config.js.
  skip: [
    ...NG_SKIP_LIST,
    /\/schematics(\/|$)/,
    '@ngbracket/ngx-layout/server',
  ],
});
