```html
@if (pdfUrl()) {
  <iframe [src]="pdfUrl()!"></iframe>
} @else {
  <p>Loading PDF...</p>
}
```
```typescript
import {Component, inject, signal, WritableSignal} from '@angular/core';
import {DomSanitizer, SafeResourceUrl} from '@angular/platform-browser';
import {HttpClient} from '@angular/common/http';

@Component({
  selector: 'app-wilhelm-tell',
    imports: [
        Book,
        MatTab,
        MatTabContent,
        MatTabGroup
    ],
  templateUrl: './wilhelm-tell.html',
  styleUrl: './wilhelm-tell.scss',
})
export class WilhelmTell {
  private readonly http = inject(HttpClient);
  private readonly sanitizer = inject(DomSanitizer);

  private blobUrl: string | null = null;
  readonly pdfUrl: WritableSignal<SafeResourceUrl | null> = signal(null);

  constructor() 
    this.loadPdf();
  }

  private loadPdf(): void {
    this.http.get('/api/v1/uploaded/wilhelm-tell.pdf', {
      responseType: 'blob',
      withCredentials: true
    }).subscribe({
      next: (blob: Blob) => {
        this.blobUrl = URL.createObjectURL(blob);
        this.pdfUrl.set(this.sanitizer.bypassSecurityTrustResourceUrl(this.blobUrl));
      },
      error: (error) => {
        console.error('Failed to load PDF:', error);
      }
    });
  }

  ngOnDestroy(): void {
      if (this.blobUrl) {
          URL.revokeObjectURL(this.blobUrl);
      }
  }
}
```
