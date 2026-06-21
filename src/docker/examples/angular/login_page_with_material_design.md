```typescript
import { Component } from '@angular/core';
import {MatCard, MatCardContent, MatCardHeader, MatCardTitle} from '@angular/material/card';
import {MatButton} from '@angular/material/button';
import {MatError, MatFormField, MatInput, MatLabel} from '@angular/material/input';
import {FormBuilder, FormGroup, ReactiveFormsModule, Validators} from '@angular/forms';
import {HttpClient} from '@angular/common/http';
import {Router} from '@angular/router';
import {AccountService} from '../../service/account-service';

@Component({
  selector: 'app-login',
  imports: [
    MatCard,
    MatCardHeader,
    MatCardTitle,
    MatCardContent,
    MatFormField,
    MatLabel,
    MatError,
    MatButton,
    MatInput,
    ReactiveFormsModule
  ],
  templateUrl: './login.html',
  styleUrl: './login.scss',
})
export class Login {

  loginForm!: FormGroup;

  constructor(private formBuilder: FormBuilder, private httpClient: HttpClient, private router: Router, private accountService: AccountService) {
    this.initializeForm();
  }

  private initializeForm(): void {
    this.loginForm = this.formBuilder.group({
      email: ['', [Validators.required, Validators.email]],
      password: ['', [Validators.required]],
      totpCode: ['', [Validators.required, Validators.pattern(/^\d{6}$/)]]
    });
  }

  onSubmit(): void {
    this.markFormGroupTouched();
    if (this.loginForm.valid) {
      const loginData = {
        email: this.loginForm.value.email,
        password: this.loginForm.value.password,
        totp: this.loginForm.value.totpCode
      };
      this.httpClient.post('/api/v1/login', loginData).subscribe({
        next: (response: any) => {
          if (response.success) {
            this.accountService.update();
            this.router.navigate(['/']).then(navigated => {
              if (!navigated) {
                console.error('Navigation failed');
              }
            });
          }
        },
        error: (error) => {
          console.error('Login error:', error);
        }
      });
    }
  }

  private markFormGroupTouched(): void {
    Object.keys(this.loginForm.controls).forEach(key => {
      const control = this.loginForm.get(key);
      control?.markAsTouched();
    });
  }

  get email() { return this.loginForm.get('email'); }
  get password() { return this.loginForm.get('password'); }
  get totpCode() { return this.loginForm.get('totpCode'); }
}
```
```html
<mat-card>
  <mat-card-header>
    <mat-card-title>Anmeldung für CHCloud</mat-card-title>
  </mat-card-header>
  <mat-card-content>
    <form [formGroup]="loginForm" (ngSubmit)="onSubmit()">
      <mat-form-field appearance="outline">
        <mat-label>E-Mail</mat-label>
        <input matInput type="email" formControlName="email" required>
        @if (email?.invalid && email?.touched) {
          <mat-error>
            Bitte geben Sie eine gültige E-Mail-Adresse ein.
          </mat-error>
        }
      </mat-form-field>

      <mat-form-field appearance="outline">
        <mat-label>Passwort</mat-label>
        <input matInput type="password" formControlName="password" required>
        @if (password?.invalid && password?.touched) {
          <mat-error>
            Bitte geben Sie Ihr Passwort ein.
          </mat-error>
        }
      </mat-form-field>

      <mat-form-field appearance="outline">
        <mat-label>TOTP Code</mat-label>
        <input matInput type="text" formControlName="totpCode" required maxlength="6">
        @if (totpCode?.invalid && totpCode?.touched) {
          <mat-error>
            Bitte geben Sie Ihren TOTP Code ein.
          </mat-error>
        }
      </mat-form-field>

      <button mat-raised-button color="primary" type="submit" [disabled]="loginForm.invalid">Anmelden</button>
    </form>
  </mat-card-content>
</mat-card>
```
```scss
:host {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  background: $app-gradient;
  padding: 20px;
}

mat-card {
  width: 100%;
  max-width: 400px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
  border-radius: 16px;
  backdrop-filter: blur(10px);
  background: rgba(255, 255, 255, 0.95);
}

mat-card-header {
  text-align: center;
  margin-bottom: 24px;
}

mat-card-title {
  font-size: 24px;
  font-weight: 500;
  color: var(--mat-sys-primary);
}

mat-card-content {
  padding: 0 24px 24px 24px;
}

form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

mat-form-field {
  width: 100%;
}

button[type="submit"] {
  margin-top: 8px;
  height: 48px;
  border-radius: 8px;
  font-size: 16px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

// Responsive design
@media (max-width: 480px) {
  :host {
    padding: 16px;
  }

  mat-card {
    max-width: none;
  }

  mat-card-content {
    padding: 0 16px 16px 16px;
  }
}
```
