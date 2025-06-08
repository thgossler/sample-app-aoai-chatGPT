// sanitizeHtml.ts
// Utility to sanitize HTML using DOMPurify
import DOMPurify from 'dompurify';
import { XSSAllowTags, XSSAllowAttributes } from '../constants/sanatizeAllowables'

export function sanitizeHtml(dirty: string): string {
  return DOMPurify.sanitize(dirty, {
    ALLOWED_TAGS: XSSAllowTags,
    ALLOWED_ATTR: XSSAllowAttributes
  })
}
