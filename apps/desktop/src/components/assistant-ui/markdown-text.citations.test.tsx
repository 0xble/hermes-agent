import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { MarkdownTextContent } from './markdown-text'

afterEach(cleanup)

describe('MarkdownTextContent citations', () => {
  it('keeps brackets visible around numeric citation links', () => {
    render(
      <MarkdownTextContent
        isRunning={false}
        text="A grounded claim.[1](https://example.com/source)"
      />
    )

    const citation = screen.getByRole('link', { name: '[1]' })

    expect(citation.getAttribute('href')).toBe('https://example.com/source')
    expect(citation.textContent).toBe('[1]')
  })

  it('does not add brackets to ordinary authored link labels', () => {
    render(<MarkdownTextContent isRunning={false} text="Read the [source](https://example.com/source)." />)

    expect(screen.getByRole('link', { name: 'source' }).textContent).toBe('source')
    expect(screen.queryByRole('link', { name: '[source]' })).toBeNull()
  })
})
