// SPDX-License-Identifier: MIT
/*
 * descriptor.xml: an application's description. karotzVM only reads the
 * accesses it is granted, <descriptor><accesses><access>led</access>...,
 * which decide the objects its JavaScript gets (see app.cc).
 */
#include <stdlib.h>
#include <string.h>

#include <libxml/parser.h>
#include <libxml/tree.h>
#include <libxml/xpath.h>

#include "karotzvm.h"

/* A descriptor that cannot be parsed ends the child, quietly. */
Descriptor::Descriptor(const char *path) : document(NULL), root(NULL), accesses(NULL)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "loading descriptor");
    document = xmlReadFile(path, NULL, 0);
    if (document == NULL)
        exit(0);
    root = xmlDocGetRootElement(static_cast<xmlDocPtr>(document));
    xmlXPathContextPtr context = xmlXPathNewContext(static_cast<xmlDocPtr>(document));
    accesses = xmlXPathEvalExpression(BAD_CAST "/descriptor/accesses/access", context);
    xmlXPathFreeContext(context);
}

Descriptor::~Descriptor()
{
    if (document != NULL)
        xmlFreeDoc(static_cast<xmlDocPtr>(document));
    if (accesses != NULL)
        xmlXPathFreeObject(static_cast<xmlXPathObjectPtr>(accesses));
}

/*
 * Compares name with the text of each <access> element (the element's
 * first child's content). An empty <access/>, or one whose first child
 * isn't text, grants nothing.
 */
bool Descriptor::has_access(const char *name) const
{
    xmlNodeSetPtr nodes = static_cast<xmlXPathObjectPtr>(accesses)->nodesetval;
    int count = nodes == NULL ? 0 : nodes->nodeNr;

    for (int i = 0; i < count; i++) {
        xmlNodePtr child = nodes->nodeTab[i]->children;
        if (child == NULL || child->content == NULL)
            continue;
        const char *granted = reinterpret_cast<const char *>(child->content);
        if (strcmp(name, granted) == 0)
            return true;
    }
    return false;
}
