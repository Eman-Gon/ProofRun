import { Routes } from '@angular/router';
import { ProofRunComponent } from './proofrun.component';

export const Extension: Routes = [
  { path: '', component: ProofRunComponent },
  { path: 'view/:id', component: ProofRunComponent },
];
