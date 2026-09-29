import { Routes } from '@angular/router';
import { ProofRunComponent } from './proofrun.component';
import { ReleaseInvestigationComponent } from './release-investigation.component';

export const Extension: Routes = [
  { path: 'releases', component: ReleaseInvestigationComponent },
  { path: 'releases/:id', component: ReleaseInvestigationComponent },
  { path: '', component: ProofRunComponent },
  { path: 'view/:id', component: ProofRunComponent },
];
