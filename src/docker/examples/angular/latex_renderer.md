Make sure to install the `katex` package before using this component.
```html
<ng-content></ng-content>
```
```scss
// KaTeX styles for LaTeX rendering in markdown
@import 'katex/dist/katex.min.css';
```
```typescript
import {Component, ElementRef, Input, Renderer2, OnChanges, SimpleChanges} from '@angular/core';
import katex from 'katex';

@Component({
  selector: 'app-latex',
  imports: [],
  templateUrl: './latex.html',
  styleUrl: './latex.scss',
})
export class Latex implements OnChanges {
  @Input() content?: string;
  @Input() mode: 'auto' | 'inline' | 'block' = 'auto';

  constructor(
    private host: ElementRef<HTMLElement>,
    private renderer: Renderer2
  ) {}

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['content'] || changes['mode']) {
      this.render();
    }
  }

  private render(): void {
    const el = this.host.nativeElement;
    const raw = (this.content ?? el.textContent ?? '').trim();

    if (!raw) {
      el.innerHTML = '';
      return;
    }

    // 1. Determine mode
    let { displayMode, latex } = this.parseLatex(raw);

    // 2. Override if mode is explicitly set
    if (this.mode !== 'auto') {
      displayMode = this.mode === 'block';
    }

    // 3. Update host styling
    this.renderer.setStyle(el, 'display', displayMode ? 'block' : 'inline');

    try {
      katex.render(latex, el, {
        displayMode,
        throwOnError: false,
      });
    } catch (err) {
      console.error('KaTeX render error:', err);
      el.textContent = raw;
    }
  }

  private parseLatex(raw: string): { displayMode: boolean; latex: string } {
    // Check for Block delimiters: $$...$$ or \[...\]
    if ((raw.startsWith('$$') && raw.endsWith('$$')) || 
        (raw.startsWith('\\[') && raw.endsWith('\\]'))) {
      return { 
        displayMode: true, 
        latex: raw.slice(2, -2).trim() 
      };
    }

    // Check for Inline delimiters: $...$ or \(...\)
    if ((raw.startsWith('$') && raw.endsWith('$')) || 
        (raw.startsWith('\\(') && raw.endsWith('\\)'))) {
      return { 
        displayMode: false, 
        latex: raw.slice(2, -2).trim() // handles \$ and \(
      };
    }

    // Fallback: Check for multiline or specific environments
    const isBlock = raw.includes('\n') || 
                    raw.includes('\\begin{') || 
                    raw.includes('\\[');

    return { displayMode: isBlock, latex: raw };
  }
}
```
