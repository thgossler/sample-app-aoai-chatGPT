export function fillPlaceholders(text: string): string {
    return text.replaceAll(/\$\(\s*([a-zA-Z0-9_]+)\s*\)/gi, (match: string, str: string, pos: string): string => {
        switch (str.toUpperCase()) {
            case 'YEAR':
                return new Date().getFullYear().toString();
            default:
                return match; // Return the original match if no placeholder is found
        }
    });
}
