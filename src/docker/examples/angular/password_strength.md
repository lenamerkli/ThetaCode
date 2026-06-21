Both HTML and TypeScript are only snippets of the full code.
```html
<mat-form-field [style.width.em]="24">
  <mat-label>Password</mat-label>
  <input matInput placeholder="****************" type="password" [formControl]="password" (keyup)="updateRating()" (blur)="updateErrorMessagePassword()" id="password">
  @if (password.invalid){
    <mat-error>{{errorMessagePassword}}</mat-error>
  }
  <mat-hint #rating>Sicherheit: {{ratingMessage}}</mat-hint>
</mat-form-field>
```
```typescript
import {zxcvbn} from "@zxcvbn-ts/core";

updateRating() {
  let inputs = this.getUserInputs();
  let input: string[] = [];
  for(let i=0; i<this.textInputs.length; i++){
    if(!this.textInputs[i].includes('password')){
      input.push(inputs[this.textInputs[i]]);
    }
  }
  let value = zxcvbn(inputs['password'], input).guessesLog10;
  if (value < 5) {
    this.ratingMessage = 'sehr schlecht';
  } else if (value < 10) {
    this.ratingMessage = 'schlecht';
  } else if (value < 15) {
    this.ratingMessage = 'akzeptabel';
  } else if (value >= 15) {
    this.ratingMessage = 'sicher';
  } else {
    this.ratingMessage = '';
  }
}
```
